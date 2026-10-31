"""AWS credentials for a Bedrock model or knowledge base connection.

One interface for every caller: given a connection's region, key pair and
role ARN, return the keyword arguments a boto3 client or ChatBedrockConverse
takes (aws_access_key_id, aws_secret_access_key, aws_session_token).

- With a key pair: the pair, or, when a role ARN is set, temporary
  credentials for that role assumed with the pair, as AI Toolkit does for its
  own Bedrock calls.
- Without a key pair: MissingKeys. Credentials are never taken from
  splunkd's environment or the host instead.
"""

# AI Toolkit's own Bedrock calls use this session name when assuming the role.
ROLE_SESSION_NAME = "splunk-ai-commander-session-name"


class MissingKeys(ValueError):
    """The connection has no access key ID or no secret key."""


class AssumeRoleFailed(Exception):
    """AssumeRole was refused or failed; `code` is the AWS error code or the exception type."""

    def __init__(self, role_arn, code, cause):
        super().__init__(f"assuming role {role_arn} failed ({code})")
        self.role_arn, self.code, self.cause = role_arn, code, cause


def for_connection(region, key_id, secret, role_arn, config):
    """Credentials to call Bedrock with for one connection; `config` is the botocore Config for STS."""
    if not (key_id and secret):
        raise MissingKeys("no access key pair")
    keys = {"aws_access_key_id": key_id, "aws_secret_access_key": secret, "aws_session_token": None}
    if not role_arn:
        return keys
    return _assumed(region, keys, role_arn, config)


def _assumed(region, source, role_arn, config):
    import boto3

    session = boto3.Session(region_name=region, **source)
    try:
        found = session.client("sts", config=config).assume_role(RoleArn=role_arn,
                                                                  RoleSessionName=ROLE_SESSION_NAME)
    except Exception as e:
        raise AssumeRoleFailed(role_arn, reason(e), e) from None
    return {
        "aws_access_key_id": found["Credentials"]["AccessKeyId"],
        "aws_secret_access_key": found["Credentials"]["SecretAccessKey"],
        "aws_session_token": found["Credentials"]["SessionToken"],
    }


def reason(e):
    """The AWS error code, or the exception type: never the message, which can echo the request."""
    aws = getattr(e, "response", None)
    if isinstance(aws, dict) and aws.get("Error", {}).get("Code"):
        return aws["Error"]["Code"]
    return type(e).__name__
