#!/usr/bin/env python3
"""CDK app: Mycel public relays in one AWS region.

    cd deploy/relay/cdk && pip install -r requirements.txt
    cdk synth -c domain=relay.example.org -c hostedZoneId=Z... -c zoneName=example.org -c relays='["a","b"]'

Deploy one stack per region you want relays in. See ../README.md before deploying anything.
"""
import json
import os

import aws_cdk as cdk

from stack import RelayStack

app = cdk.App()


def ctx(key, default=None):
    v = app.node.try_get_context(key)
    return default if v in (None, "") else v


relays = ctx("relays", ["a"])
if isinstance(relays, str):
    relays = json.loads(relays)

RelayStack(
    app, ctx("stackName", "MycelRelays"),
    domain=ctx("domain"), hosted_zone_id=ctx("hostedZoneId"), zone_name=ctx("zoneName"),
    relay_names=list(relays), alarm_email=ctx("alarmEmail"),
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
                        region=ctx("region") or os.environ.get("CDK_DEFAULT_REGION")),
)
app.synth()
