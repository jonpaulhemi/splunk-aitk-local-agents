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
An Amazon Bedrock knowledge base attached to an agent, which the agent can search for passages. AI Toolkit calls its connection a "Vector DB" of type AWS_KB.
_Avoid_: vector DB, KB connection, RAG source

**Knowledge base search**:
One query the agent sends to a knowledge base. It returns passages and where they came from, not a written answer.
_Avoid_: retrieval, RAG call
