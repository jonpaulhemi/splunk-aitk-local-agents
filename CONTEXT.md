# AITK Local Agent Runtime

Runs AI Toolkit agents on the search head instead of in Splunk's cloud. This glossary covers the words we use for what an agent is given and what it does with it.

## Skills

**Skill**:
A named set of written instructions that someone wrote in AI Toolkit and attached to an agent, with a short description of when it applies.
_Avoid_: prompt, playbook, instruction set

**Loaded skill**:
A skill whose full instructions the agent chose to read during a run. Attaching a skill to an agent does not load it.
_Avoid_: used skill, active skill

**Managed skill**:
A skill from Splunk's hosted catalog. Offline, the catalog is empty, so agents only have skills written locally.
_Avoid_: built-in skill, catalog skill

## Knowledge bases

**Knowledge base**:
An Amazon Bedrock knowledge base attached to an agent, which the agent can search for chunks. AI Toolkit calls its connection a "Vector DB" of type AWS_KB.
_Avoid_: vector DB, KB connection, RAG source

**Knowledge base search**:
One query the agent sends to a knowledge base. It returns the best-matching chunks and where they came from, not a written answer.
_Avoid_: retrieval, RAG call

**Chunk**:
A small piece of one document in a knowledge base, a few paragraphs long. A search returns chunks, not whole documents.
_Avoid_: passage, snippet, result

## AWS credentials

**Instance-role connection**:
A Bedrock model or knowledge base connection with a role ARN and no access keys. Its credentials come from the search head's EC2 instance profile (through IMDSv2), which assumes the role, usually in another AWS account. Made on Cloud Connect's setup page, and named with an "(instance role)" suffix.
_Avoid_: keyless connection, IMDS connection

**Instance profile**:
The IAM role attached to the search head's EC2 instance. For instance-role connections it only needs permission to assume the connections' roles.
_Avoid_: instance role (for the profile itself), host credentials
