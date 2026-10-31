"""A small fake of splunkd's management port, for contract tests.

It serves the persistent REST handlers declared in each installed app's
restmap.conf (ours and AI Toolkit's) the way splunkd does: it authenticates the
caller, turns the HTTP request into splunkd's persistent-handler JSON, and turns
the handler's reply back into HTTP. It also answers the handful of built-in
splunkd endpoints that the code under test calls (server info, users, current
context, storage/passwords).

Only behaviour the tests rely on is modelled. Everything observed on a real
search head in spike #2 is reproduced: requireAuthentication = false passes a
non-Splunk Authorization header through untouched, a handler matched at a
prefix sees the remainder in `path_info`, and responses are returned whole.
"""

import configparser
import http.server
import importlib
import inspect
import json
import secrets
import socketserver
import ssl
import sys
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

SYSTEM_USER = "splunk-system-user"

NOT_FOUND = (404, {}, {"messages": [{"type": "ERROR", "text": "Not Found"}]})
UNAUTHORIZED_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<response>\n  <messages>\n'
    '    <msg type="ERROR">Unauthorized</msg>\n  </messages>\n</response>\n'
)


@dataclass
class User:
    name: str
    capabilities: list
    roles: list = field(default_factory=list)


@dataclass
class Route:
    app: str
    match: str
    handler: object
    require_auth: bool
    pass_system_auth: bool
    pass_headers: bool
    pass_payload: bool


def _read_conf(app_dir, name):
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    for layer in ("default", "local"):
        path = app_dir / layer / name
        if path.exists():
            # Splunk continues a value onto the next line with a trailing backslash.
            parser.read_string(path.read_text().replace("\\\n", " "), source=str(path))
    return parser


def _truthy(value, default):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "t", "yes", "y")


def _find_handler_class(module, base_name="PersistentServerConnectionApplication"):
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj.__module__ == module.__name__ and any(
            b.__name__ == base_name for b in obj.__mro__[1:]
        ):
            return obj
    raise LookupError(f"no persistent handler class in {module.__name__}")


class FakeSplunkd:
    def __init__(self, splunk_home: Path, tls: dict, hostname="localhost"):
        self.splunk_home = splunk_home
        self.tls = tls
        self.hostname = hostname
        self.users = {SYSTEM_USER: User(SYSTEM_USER, ["*"], ["splunk-system-role"])}
        self.tokens = {}
        self.system_token = self.login(SYSTEM_USER)
        self.passwords = {}  # (app, realm, name) -> clear password
        self.fail_credential_store = False  # simulate a broken storage/passwords
        self.kvstore = {}  # (app, collection) -> {_key: document}
        # Settings written over REST: (app, conf, stanza) -> {key: value}. Apps'
        # own conf files are only read, never written (AI Toolkit's is a symlink
        # to the real unpacked app).
        self.conf_writes = {}
        self.routes = []
        self._lock = threading.Lock()
        self._server = None

    # -- identities -----------------------------------------------------

    def add_user(self, name, capabilities, roles=()):
        self.users[name] = User(name, list(capabilities), list(roles))
        return self.login(name)

    def login(self, name):
        token = secrets.token_hex(16)
        self.tokens[token] = name
        return token

    def _user_for(self, auth_header):
        if not auth_header:
            return None, None
        scheme, _, token = auth_header.partition(" ")
        if scheme not in ("Splunk", "Bearer"):
            return None, None
        name = self.tokens.get(token.strip())
        return (self.users.get(name), token.strip()) if name else (None, None)

    # -- apps -----------------------------------------------------------

    def load_routes(self):
        self.routes = []
        apps_dir = self.splunk_home / "etc" / "apps"
        for app_dir in sorted(p for p in apps_dir.iterdir() if p.is_dir()):
            restmap = _read_conf(app_dir, "restmap.conf")
            for stanza in restmap.sections():
                if not stanza.startswith("script:"):
                    continue
                conf = restmap[stanza]
                if conf.get("scripttype") != "persist":
                    continue
                script = conf.get("script")
                if not script:
                    # Spike #2: splunkd silently skips persist handlers without script=.
                    continue
                bin_dir = str(app_dir / "bin")
                if bin_dir not in sys.path:
                    sys.path.insert(0, bin_dir)
                module_name = script[:-3].replace("/", ".") if script.endswith(".py") else script
                handler_spec = conf.get("handler")
                if handler_spec:
                    mod_name, _, cls_name = handler_spec.rpartition(".")
                    cls = getattr(importlib.import_module(mod_name), cls_name)
                else:
                    # AI Toolkit's [script:mltk] names only the script.
                    cls = _find_handler_class(importlib.import_module(module_name))
                self.routes.append(
                    Route(
                        app=app_dir.name,
                        match=conf.get("match").rstrip("/"),
                        handler=cls(None, None),
                        require_auth=_truthy(conf.get("requireAuthentication"), True),
                        pass_system_auth=_truthy(conf.get("passSystemAuth"), False),
                        pass_headers=_truthy(conf.get("passHttpHeaders"), False),
                        pass_payload=_truthy(conf.get("passPayload"), False),
                    )
                )
        # Longest match wins, like splunkd.
        self.routes.sort(key=lambda r: len(r.match), reverse=True)

    def installed_apps(self):
        return {p.name for p in (self.splunk_home / "etc" / "apps").iterdir() if p.is_dir()}

    # -- server ---------------------------------------------------------

    @property
    def port(self):
        return self._server.server_address[1]

    @property
    def uri(self):
        return f"https://{self.hostname}:{self.port}"

    def start(self):
        self.load_routes()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, headers, payload = fake.dispatch(
                    self.command, self.path, dict(self.headers.items()), body
                )
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload)
                    headers.setdefault("Content-Type", "application/json; charset=UTF-8")
                data = payload.encode() if isinstance(payload, str) else (payload or b"")
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _serve

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.tls["cert"], self.tls["key"])
        self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # -- routing --------------------------------------------------------

    def dispatch(self, method, raw_path, headers, body):
        parsed = urllib.parse.urlsplit(raw_path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] not in ("services", "servicesNS"):
            return NOT_FOUND
        if parts[0] == "servicesNS":
            if len(parts) < 3:
                return NOT_FOUND
            ns_user, ns_app, rest = parts[1], parts[2], parts[3:]
            if ns_app not in self.installed_apps() and ns_app != "-":
                return NOT_FOUND
        else:
            ns_user, ns_app, rest = None, None, parts[1:]
        rest_path = "/" + "/".join(rest)

        user, token = self._user_for(headers.get("Authorization"))

        route = next(
            (r for r in self.routes if rest_path == r.match or rest_path.startswith(r.match + "/")),
            None,
        )
        if route is not None:
            if route.require_auth and user is None:
                return self._unauthorized()
            return self._call_handler(
                route, method, rest_path, query, headers, body, user, token, ns_user, ns_app
            )

        if user is None:
            return self._unauthorized()
        return self._builtin(method, rest, query, body, user, ns_app)

    @staticmethod
    def _unauthorized():
        return 401, {"Content-Type": "text/xml; charset=UTF-8"}, UNAUTHORIZED_XML

    def _call_handler(self, route, method, rest_path, query, headers, body, user, token, ns_user, ns_app):
        request = {
            "output_mode": dict(query).get("output_mode", "xml"),
            "rest_path": rest_path,
            "path_info": rest_path[len(route.match):].lstrip("/"),
            "method": method,
            "query": [list(kv) for kv in query],
            "form": [list(kv) for kv in urllib.parse.parse_qsl(body.decode())] if method == "POST" else [],
            "ns": {"app": ns_app, "user": ns_user},
            "session": {"user": user.name if user else None, "authtoken": token if user else None},
            "server": {
                "rest_uri": self.uri,
                "hostname": self.hostname,
                "servername": self.hostname,
                "guid": "00000000-0000-0000-0000-000000000000",
            },
            "connection": {"src_ip": "127.0.0.1", "listening_port": self.port, "ssl": True},
        }
        if route.pass_headers:
            request["headers"] = [[k, v] for k, v in headers.items()]
        if route.pass_payload:
            request["payload"] = body.decode("utf-8", "replace")
        if route.pass_system_auth:
            request["system_authtoken"] = self.system_token
        reply = route.handler.handle(json.dumps(request))
        if isinstance(reply, (str, bytes)):
            reply = json.loads(reply)
        status = int(reply.get("status", 200))
        payload = reply.get("payload", "")
        return status, dict(reply.get("headers") or {}), payload

    # -- built-in splunkd endpoints ----------------------------------------

    def _builtin(self, method, rest, query, body, user, ns_app):
        if rest == ["server", "info"] and method == "GET":
            # No instance_type: AI Toolkit treats the instance as on-prem.
            return 200, {}, {"entry": [{"content": {"serverName": self.hostname}}]}

        if rest == ["authentication", "current-context"] and method == "GET":
            return 200, {}, {
                "entry": [
                    {
                        "content": {
                            "username": user.name,
                            "capabilities": self._capabilities(user),
                            "roles": user.roles,
                        }
                    }
                ]
            }

        if len(rest) == 3 and rest[:2] == ["authentication", "users"] and method == "GET":
            target = self.users.get(rest[2])
            if target is None:
                return NOT_FOUND
            return 200, {}, {
                "entry": [
                    {"content": {"capabilities": self._capabilities(target), "roles": target.roles}}
                ]
            }

        if rest[:2] == ["storage", "passwords"] and ns_app:
            return self._passwords(method, rest[2:], body, user, ns_app)

        if rest[:3] == ["storage", "collections", "data"] and len(rest) >= 4 and ns_app:
            return self._kvstore(method, rest[3], rest[4:], query, body, ns_app)

        if ns_app and ns_app != "-":
            form = dict(urllib.parse.parse_qsl(body.decode())) if method == "POST" else {}
            if method in ("POST", "DELETE") and "admin_all_objects" not in self._capabilities(user):
                return 403, {}, {"messages": [{"type": "ERROR", "text": "Forbidden"}]}
            if len(rest) >= 2 and rest[0] == "configs" and rest[1].startswith("conf-"):
                return self._conf_endpoint(method, ns_app, rest[1][5:], rest[2:], form)
            if rest[:2] == ["properties", "mlspl"] and len(rest) == 3 and method == "GET":
                return 200, {}, {"entry": [{"name": k, "content": v}
                                           for k, v in self.conf_stanza(ns_app, "mlspl.conf", rest[2]).items()]}
            if rest[:2] == ["data", "inputs"] and len(rest) in (4, 5):
                stanza = f"{rest[2]}://{rest[3]}"
                if len(rest) == 5:
                    if method != "POST" or rest[4] not in ("disable", "enable"):
                        return NOT_FOUND
                    form = {"disabled": "1" if rest[4] == "disable" else "0"}
                return self._conf_entry(method, ns_app, "inputs.conf", stanza, form,
                                        lambda c: {"disabled": _truthy(c.get("disabled"), False)})
            if rest[:2] == ["saved", "searches"] and len(rest) == 3:
                if "is_scheduled" in form:
                    form = {"enableSched": form.pop("is_scheduled"), **form}
                return self._conf_entry(method, ns_app, "savedsearches.conf", rest[2], form,
                                        lambda c: {"is_scheduled": _truthy(c.get("enableSched"), False)})

        return NOT_FOUND

    def conf_stanza(self, app, conf, stanza):
        """A stanza as splunkd would merge it: the app's default/ and local/, then REST writes."""
        parser = _read_conf(self.splunk_home / "etc" / "apps" / app, conf)
        merged = dict(parser[stanza]) if parser.has_section(stanza) else None
        writes = self.conf_writes.get((app, conf, stanza))
        if merged is None and writes is None:
            return None
        return {**(merged or {}), **(writes or {})}

    def _conf_stanzas(self, app, conf):
        names = set(_read_conf(self.splunk_home / "etc" / "apps" / app, conf).sections())
        names |= {s for (a, c, s) in self.conf_writes if (a, c) == (app, conf)}
        return sorted(names)

    def _conf_endpoint(self, method, app, conf_name, rest, form):
        conf = f"{conf_name}.conf"
        if not rest and method == "GET":
            return 200, {}, {"entry": [{"name": s, "content": self.conf_stanza(app, conf, s)}
                                       for s in self._conf_stanzas(app, conf)]}
        if not rest and method == "POST":  # create a stanza
            stanza = form.pop("name", "")
            with self._lock:
                if not stanza or self.conf_stanza(app, conf, stanza) is not None:
                    return 409, {}, {"messages": [{"type": "ERROR", "text": "An object with name already exists"}]}
                self.conf_writes[(app, conf, stanza)] = dict(form)
            return 201, {}, {"entry": [{"name": stanza, "acl": {"app": app}, "content": dict(form)}]}
        if len(rest) == 1 and method == "DELETE":
            with self._lock:  # only stanzas made through REST can be deleted here
                if self.conf_writes.pop((app, conf, rest[0]), None) is None:
                    return NOT_FOUND
            return 200, {}, {"entry": []}
        if len(rest) == 1:
            return self._conf_entry(method, app, conf, rest[0], form, lambda c: c)
        return NOT_FOUND

    def _conf_entry(self, method, app, conf, stanza, form, view):
        """GET or POST (update) one stanza; `view` shapes its content like the endpoint does."""
        with self._lock:
            if self.conf_stanza(app, conf, stanza) is None:
                return NOT_FOUND
            if method == "POST":
                self.conf_writes.setdefault((app, conf, stanza), {}).update(form)
            elif method != "GET":
                return NOT_FOUND
            content = self.conf_stanza(app, conf, stanza)
        return 200, {}, {"entry": [{"name": stanza, "acl": {"app": app}, "content": view(content)}]}

    def _capabilities(self, user):
        if "*" in user.capabilities:
            return ["admin_all_objects", "list_storage_passwords", "apply_ai_commander_command"]
        return list(user.capabilities)

    def _passwords(self, method, rest, body, user, app):
        if self.fail_credential_store:
            return 500, {}, {"messages": [{"type": "ERROR", "text": "Internal error"}]}
        if "list_storage_passwords" not in self._capabilities(user):
            return 403, {}, {"messages": [{"type": "ERROR", "text": "Forbidden"}]}
        with self._lock:
            if method == "POST" and not rest:
                form = dict(urllib.parse.parse_qsl(body.decode()))
                key = (app, form.get("realm", ""), form["name"])
                if key in self.passwords:
                    return 409, {}, {"messages": [{"type": "ERROR", "text": "An object with name already exists"}]}
                self.passwords[key] = form["password"]
                return 201, {}, {"entry": [self._password_entry(key)]}
            if len(rest) == 1:
                realm, _, name = rest[0].rstrip(":").partition(":")
                key = (app, realm, name)
                if key not in self.passwords:
                    return NOT_FOUND
                form = dict(urllib.parse.parse_qsl(body.decode()))
                if method == "POST" and "password" in form:  # update; a bare POST reads, as in splunkd
                    self.passwords[key] = form["password"]
                elif method == "DELETE":
                    del self.passwords[key]
                    return 200, {}, {"entry": []}
                elif method not in ("GET", "POST"):
                    return NOT_FOUND
                return 200, {}, {"entry": [self._password_entry(key)]}
        return NOT_FOUND

    def _kvstore(self, method, collection, rest, query, body, app):
        """Collection data: list (with an equality `query`), get, insert, update, delete."""
        docs = self.kvstore.setdefault((app, collection), {})
        with self._lock:
            if not rest and method == "GET":
                where = json.loads(dict(query).get("query") or "{}")
                return 200, {}, [d for d in docs.values() if all(d.get(k) == v for k, v in where.items())]
            if not rest and method == "DELETE":  # delete by query, as splunkd's KV store does
                where = json.loads(dict(query).get("query") or "{}")
                for key in [k for k, d in docs.items() if all(d.get(f) == v for f, v in where.items())]:
                    del docs[key]
                return 200, {}, ""
            if not rest and method == "POST":
                doc = json.loads(body)
                key = doc.get("_key") or secrets.token_hex(12)
                docs[key] = {**doc, "_key": key}
                return 201, {}, {"_key": key}
            if len(rest) == 1 and rest[0] != "batch_save":
                key = urllib.parse.unquote(rest[0])
                if method == "GET":
                    return (200, {}, docs[key]) if key in docs else NOT_FOUND
                if method == "POST":
                    docs[key] = {**json.loads(body), "_key": key}
                    return 200, {}, {"_key": key}
                if method == "DELETE":
                    return (200, {}, "") if docs.pop(key, None) else NOT_FOUND
        return NOT_FOUND

    def _password_entry(self, key):
        app, realm, name = key
        return {
            "name": f"{realm}:{name}:",
            "acl": {"app": app},
            "content": {"realm": realm, "username": name, "clear_password": self.passwords[key]},
        }
