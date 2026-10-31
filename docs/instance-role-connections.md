# Instance-role connections

From 0.13.0, Bedrock model and knowledge base connections can run with no access keys. Instead, the search head's EC2 instance profile assumes an IAM role, usually in another AWS account where Bedrock lives, and every Bedrock call runs as that role. These are **instance-role connections**. You make them on Cloud Connect's setup page, not in AI Toolkit's connection form.

## How it works

1. The runtime gets the instance profile's credentials from the EC2 instance metadata service, IMDSv2 only. It never uses `AWS_*` environment variables or `~/.aws` profiles for these connections, so a stray key on the host can't change who calls Bedrock.
2. It assumes the connection's role on the STS endpoint in the connection's region. It assumes the role afresh on every agent run and logs the role ARN (`assumed role <arn> with the instance credentials` in `aitk_local.log`).
3. Model calls (Converse) and knowledge base searches (Retrieve) run as that role.

A connection with access keys still works exactly as before, and keys win when a connection has both.

## 1. IAM setup

You need three pieces: the search head's instance profile in account A, a role in the Bedrock account B, and a trust policy linking them. A and B can also be the same account.

**Instance profile** (account A). Attach a role to the search head's EC2 instance with only this permission policy. List each role your connections use:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AssumeTheBedrockRoles",
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": [
      "arn:aws:iam::<account-B>:role/bedrock-invoke",
      "arn:aws:iam::<account-B>:role/kb-search"
    ]
  }]
}
```

**Role in the Bedrock account** (account B). Give it what its connections need, scoped by ARN where Bedrock allows it:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "CallModels", "Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
     "Resource": ["arn:aws:bedrock:<region>::foundation-model/*",
                  "arn:aws:bedrock:<region>:<account-B>:inference-profile/*"]},
    {"Sid": "SearchKnowledgeBases", "Effect": "Allow",
     "Action": "bedrock:Retrieve",
     "Resource": "arn:aws:bedrock:<region>:<account-B>:knowledge-base/<kb-id>"},
    {"Sid": "ListModelsForTheSetupPage", "Effect": "Allow",
     "Action": ["bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles"],
     "Resource": "*"}
  ]
}
```

- **CallModels** is for model connections. Converse is authorized as `bedrock:InvokeModel`. A cross-region inference profile also needs the foundation model in each region it routes to.
- **SearchKnowledgeBases** is for knowledge base connections. It's also needed on the *model* connection's role if you use `| ai ... kb_id=` (see Known limits).
- **ListModelsForTheSetupPage** is optional. Without it, the setup page's model list fails and you type the model ID instead.

**Trust policy** on that role. Name the instance profile's role exactly. No ExternalId: AI Toolkit's own `| ai` can't send one.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::<account-A>:role/<instance profile role>"},
    "Action": "sts:AssumeRole"
  }]
}
```

**IMDS.** The instance must allow IMDSv2 (the default for new instances, and "IMDSv2 required" is fine). If Splunk runs in a container on the instance, set the instance's metadata hop limit to 2, or the container can't reach IMDS.

## 2. Make the connections

Open the **AITK Local Agent Runtime** app (Cloud Connect) in Splunk Web as an admin (`admin_all_objects`). Its one page, **Instance-role connections**, lists the connections it made and creates, edits and deletes them.

- **Name:** letters, digits and underscores, AI Toolkit's own rule. The page adds an `_instance_role` suffix, so these connections stand out in Launchpad's pickers and in AI Toolkit's connection list.
- **Region and role ARN:** the role must look like `arn:<partition>:iam::<12-digit account>:role/<name>`.
- **Model connections:** pick the model with **List models**, which lists what the role can see in that region. Or type the model or inference profile ID. You can also set temperature, max tokens and request timeout.
- **Knowledge bases:** the 10-character knowledge base ID, and a description. The description is all the agent learns about the knowledge base, so say what's in it and when to search it.

Every save is tested first. For a model, the test assumes the role and makes a one-token Converse call. For a knowledge base, it assumes the role and runs one Retrieve. If the test fails, the page says which step failed and gives the AWS error code:

- **"Couldn't assume role …"** means the trust policy or the instance profile's `sts:AssumeRole` permission is wrong.
- **"Assumed role …, but Bedrock refused the call"** (or "the knowledge base search failed") means the role's Bedrock permissions or model access are wrong.
- **"No instance credentials were found"** means no instance profile is attached, or IMDS can't be reached.

**Save anyway** saves without a passing test, for example before the role exists.

The page writes each connection into AI Toolkit's own store, with its key fields left empty. Attach it to agents in Launchpad like any other connection.

The same actions are available over REST, for scripting. They're admin only, and every body is JSON:

```
GET    /services/aitk_local/connections
POST   /services/aitk_local/connections/models            {"name", "region", "role_arn", "model", "temperature", "max_tokens", "request_timeout", "description", "save_anyway"}
POST   /services/aitk_local/connections/knowledge_bases   {"name", "region", "role_arn", "kb_id", "description", "save_anyway"}
POST   /services/aitk_local/connections/<kind>/test       (same body; tests without saving)
POST   /services/aitk_local/connections/models/available  {"region", "role_arn"}
POST   /services/aitk_local/connections/<kind>/<name>     (edit; same body)
DELETE /services/aitk_local/connections/<kind>/<name>
```

## Replacing a key-based connection

The page doesn't convert connections. To move from access keys to the instance role:
1. Create the instance-role connection on the page.
2. Switch your agents to it in Launchpad.
3. Delete the old connection in AI Toolkit.

## Known limits

- **AI Toolkit's Test connection button** always fails for instance-role connections, for both models and knowledge bases, because it needs access keys. Use the page's test instead.
- **Edit these connections only on the setup page.** AI Toolkit's form asks for access keys, and saving there can turn the connection into a key-based one. The page's registry would still list it, but agents would then use the keys.
- **`| ai`:** AI Toolkit's own search command passes the empty keys to litellm, which then uses the instance profile and assumes the role. This works without changing AI Toolkit, but it's AI Toolkit's code path, so it's best effort. With `kb_id=`, `| ai` searches the knowledge base as the *model* connection's role, not the knowledge base connection's. Give that role `bedrock:Retrieve` on the knowledge base.
- **Who sees what:** AI Toolkit makes connections private to the admin who created them, as its own form does. The page lists every instance-role connection it made. Editing or deleting another admin's connection fails if AI Toolkit doesn't let you change it.
- **GovCloud and C2S:** the design is partition-neutral. It uses regional STS and accepts any `arn:aws*` partition in role ARNs, but it has only been tested in commercial AWS.
