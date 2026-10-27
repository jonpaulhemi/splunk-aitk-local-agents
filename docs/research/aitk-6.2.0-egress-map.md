# AI Toolkit 6.2.0 (Splunk_ML_Toolkit): outbound connection map

Source: `splunk-ai-toolkit_620.tgz` (app id `Splunk_ML_Toolkit`, version 6.2.0, build 1791891005831). Found by static review of `bin/` and `default/`, not by running it.

## Scheduled / automatic (fires with no user action)
| What | Where | Goes to | Off switch |
|---|---|---|---|
| Agent template sync (every 6h) | `default/inputs.conf` `[slim_agent_templates://managed_templates]`, `bin/connection_config_manager/templates/slim_template_sync.py` | Splunk Cloud Services "SLIM" API `https://{tenant}/.../slim-api/v1alpha1`, via Cloud Connect | `disabled = 1` in `local/inputs.conf` |
| Managed skills catalog sync (every 6h) | `[splunk_managed_skills://catalog]`, `bin/connection_config_manager/skills/managed_skill_sync.py` | SLIM API | `disabled = 1` |
| Telemetry, 5 saved searches (ML-SPL, FineTuneCDTSM x2, AI Agent, Sourcetype Inference) | `default/savedsearches.conf` | `action.outputtelemetry` → Splunk's instrumentation app → Splunk | `enableSched = 0` or `action.outputtelemetry = 0` in `local/` |

## On use (fires when someone runs a feature)
| Feature | Default destination | Local option already in the app? |
|---|---|---|
| `\| ai` and AI agents (`aiagent`) | OpenAI, Azure OpenAI, Anthropic, Gemini, Groq, Bedrock | **Yes: Ollama provider and a CUSTOM provider** (`bin/ai_commander/llm_factory.py`) |
| CDTSM time-series forecasting | Splunk-hosted SLIM model (default, step 4 of `resolve_forecast_provider`) | **Yes: self-hosted endpoint in `mlspl.conf [CTSM]`**, takes precedence |
| AI Tier shared endpoint | KV store saved endpoint | Points wherever you configure |
| MCP connections from agents | Atlassian, PagerDuty, custom | Custom URL; auth types OAuth2, headers, basic, API key, bearer. **No client-certificate (mTLS) option**, and `verify=False` is hardcoded in `list_tools.py` |
| SageMaker / Vertex AI model registration | AWS / Google | Feature is cloud-only |
| DSDL containers | Your container host (docker/k8s/EKS) | On-prem capable |

## Built-in guardrail
`mlspl.conf [ai:AllowedDomains]`: `allowed_domains = *`, `enforce_domain_validation = true`. Setting `allowed_domains` to only your internal hosts should block agent/LLM calls to anything else (enforced in `bin/connection_config_manager/utils/common_utils.py`). Not yet verified to cover every code path.

## Web UI
The bundled JS has no live external hosts beyond license/doc strings (momentjs, lodash, highcharts and so on). Help links point at help.splunk.com.
