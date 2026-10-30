# Setting up a small Bedrock knowledge base

This guide gets you a knowledge base you can attach to an agent, for trying out knowledge base search (0.12.0 and later). It assumes a commercial AWS account and the AWS console, and should take about 20 minutes. Everything happens in one region. Pick one where Bedrock knowledge bases are available, such as `us-east-1` or `us-west-2`, and use it throughout.

## 1. Put a few documents in S3

1. Create an S3 bucket, for example `aitk-kb-test-<your initials>`, with the default settings (public access blocked).
2. Upload three to five short documents the agent can answer questions from. Plain text, Markdown, HTML, Word and PDF all work. Something you can check answers against is best, such as a runbook, an on-call rota, or a short policy.

## 2. Create the knowledge base

1. Open **Amazon Bedrock > Knowledge Bases > Create > Knowledge base with vector store**.
2. Name it, for example `aitk-kb-test`. Under service role, let Bedrock create one. That role is for Bedrock's own ingestion, not for the search head.
3. Data source: **Amazon S3**, pointing at your bucket. Keep the default parsing and the default chunking, which cuts documents into chunks of about 300 tokens.
4. Embeddings model: **Titan Text Embeddings V2** (enable model access when the console asks).
5. Vector store: choose **Quick create a new vector store**, and pick **Amazon S3 Vectors** if it's offered. It's the cheapest option for a test. OpenSearch Serverless works too, but it bills for its minimum capacity for as long as it exists.
6. Create the knowledge base, then open its data source and click **Sync**. Wait until the sync shows as completed.
7. Copy the **Knowledge base ID** (10 characters, like `EJGJR4SKJL`) from the knowledge base's overview.
8. Optional check: use **Test knowledge base**, turn off "Generate responses", and run a query. You should see chunks from your documents.

## 3. Create the credentials the search head uses

AI Toolkit's knowledge base connection takes an access key pair and a role ARN, and both are required. Our runtime signs searches with the role: it uses the key pair to assume the role, then searches as that role. AI Toolkit's own **Test connection** button calls Bedrock with the key pair directly and ignores the role. If you want that button to pass, also give the user permission to search; agent runs don't need it.

1. **IAM role** `aitk-kb-search` with this permission policy, using your region, account ID and knowledge base ID:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": "bedrock:Retrieve",
       "Resource": "arn:aws:bedrock:<region>:<account-id>:knowledge-base/<kb-id>"
     }]
   }
   ```

   Its trust policy lets the user below assume it:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": {"AWS": "arn:aws:iam::<account-id>:user/aitk-kb-search"},
       "Action": "sts:AssumeRole"
     }]
   }
   ```

2. **IAM user** `aitk-kb-search` with no console access, and an inline policy that lets it assume the role. The second statement is optional: it only makes AI Toolkit's Test connection pass.

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {"Effect": "Allow", "Action": "sts:AssumeRole",
        "Resource": "arn:aws:iam::<account-id>:role/aitk-kb-search"},
       {"Effect": "Allow", "Action": "bedrock:Retrieve",
        "Resource": "arn:aws:bedrock:<region>:<account-id>:knowledge-base/<kb-id>"}
     ]
   }
   ```

3. Create an access key for the user (use case "Application running outside AWS") and keep the key ID and secret for the next step.

Create the user first if the console rejects the role's trust policy because the user doesn't exist yet.

## 4. Connect it in AI Toolkit

1. In AI Toolkit, open the agent connections page and add a connection with **Connection Type: Knowledge base** and **Knowledge base type: AWS KB**.
2. Fill in the region, the knowledge base ID, the access key and secret key from step 3, and the role ARN `arn:aws:iam::<account-id>:role/aitk-kb-search`.
3. **Knowledge base id description** is what the agent reads when deciding whether to search. It's the only thing the model learns about the knowledge base, so say what's in it and when to use it. For example: "Our on-call runbooks and escalation rota. Search it for how to handle an alert or who to page."
4. Click **Test connection**, then save. It fails without the optional search permission on the user, and it always fails for Bedrock's *managed* knowledge bases ("vectorSearchConfiguration is not supported for managed knowledge bases"), because AI Toolkit's test only knows vector search. Both are fine: agent runs handle managed knowledge bases.
5. Edit an agent, attach the knowledge base, and save the agent.

With knowledge base search in place (0.12.0 and later), ask the agent something only your documents answer. The run trace shows a `search_knowledge_base_<id>` call, and the answer can cite the S3 file it came from.

## Cost and teardown

Searching and syncing a handful of documents costs very little. The vector store is the one thing that keeps billing while it exists, especially OpenSearch Serverless. When you're done:

1. Delete the knowledge base, and choose to delete its vector store if the console offers it. Otherwise delete the S3 vector bucket or the OpenSearch Serverless collection yourself.
2. Delete the documents bucket.
3. Delete the user's access key, the `aitk-kb-search` user and role, and the service role Bedrock created.
4. Remove the knowledge base connection in AI Toolkit.
