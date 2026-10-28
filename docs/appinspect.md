# AppInspect

This file records how the release package fares against Splunk AppInspect 4.x, `precert` mode with the default (on-premises) checks.

## How to run it

The whole package can't be inspected in one go on a 16 GB machine. AppInspect's Python analyzer grows past 12 GB on the roughly 9,000 files in `lib/`, and then it gets killed. So the check is split:

1. **The app's own code and config.** Build the package with `lib/` emptied and inspect that.
2. **The bundled libraries.** Inspect them in batches. Each batch is the app plus some of `lib/`, split as follows:
   - `openai`
   - `botocore`
   - `numpy`
   - the LangChain, LangGraph and pydantic packages
   - everything else

numpy's Python analysis runs out of memory even on its own. Its binary checks still run.

A machine with more memory can inspect the whole .tgz directly:

```
pip install splunk-appinspect
splunk-appinspect inspect dist/splunk_cloud_connect-<version>.tgz --mode precert
```

## Results (0.10.1)

The app's own code and config have no failures.

The bundled libraries have two known failures, both kept on purpose:

- **`check_aarch64_compatibility`:** 40 compiled libraries are x86_64 only. These include numpy, pydantic-core, cryptography, cffi and charset-normalizer. The app targets Linux x86_64 search heads (see `DESIGN.md`), and `tools/bundle.py` builds for that platform only.
- **`check_for_python_udp_network_communications`:** this is `botocore/session.py`, which is AWS client-side monitoring. It only opens a UDP socket when `AWS_CSM_ENABLED` is set. It's off by default, and nothing in this app turns it on. botocore is how Bedrock is reached, so it stays.

The remaining warnings need no change:

- **Python 2/3 compatibility:** the app pins `python.required = 3.13`.
- **`sc_admin` access:** this only applies on Splunk Cloud.
- **Hostnames and IPs:** these are documentation examples in botocore's service data.
- **Subprocess and threading:** these are in library code paths the agents don't use.
- **"Insecure HTTP":** false matches on dictionary `.get()` calls.

These findings were fixed in 0.10.1:

- `app.conf` no longer marks setup as done.
- `app.conf` has an `[id]` stanza.
- `app.conf` has a reload trigger for `aitk_local.conf`.
- `server.conf` replicates `aitk_local.conf` in a search head cluster.
- Files in `lib/` aren't executable.
- The unused audio helpers in openai and langsmith, which call `wave.open`, are left out of the package.

## Upgrading in place

`splunk install app -update 1` unpacks the new package over the installed app. It doesn't delete files that the new package leaves out, and it doesn't change the modes of files that are already there. So a search head upgraded from 0.10.0 still has the audio helpers and the executable library files. They're harmless: nothing imports the helpers, and the libraries load either way.

The package itself is what AppInspect checks. To make the installed tree match it as well, follow these steps:

1. Back up `$SPLUNK_HOME/etc/apps/splunk_cloud_connect/local/`.
2. Remove `$SPLUNK_HOME/etc/apps/splunk_cloud_connect`.
3. Install the .tgz.
4. Restore `local/`.
5. Restart.

Session memory survives this, because it lives in `$SPLUNK_HOME/var/lib/aitk_local/`, outside the app.

The libraries' compiled modules were checked to load from the 0.10.1 package with no execute bits (CPython 3.13).
