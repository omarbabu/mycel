"""One region's Mycel relays.

Shape (every relay URL maps to exactly one relay process: nodes on the same URL must meet in the
same process, so each relay is its own single-task ECS service and its own host name):

    internet ──443/TLS1.3──> ALB (public subnets, WAF) ──host rule──> Fargate task per relay
                                                                    (isolated subnets, no public IP,
                                                                     no outbound internet)

* Only 443 is open, only on the load balancer. Tasks accept 7600 from the ALB's security group only.
* Tasks have no route to the internet at all (VPC endpoints for image pulls and logs only).
* WAF: AWS IP-reputation list plus a per-address rate limit on connection attempts.
* Deployments never run two tasks for one relay (max 100%): a brief reconnect instead of a
  split relay. Nodes heal onto their other relays meanwhile.
* ALB access logs (client IPs) are kept 30 days in a private, TLS-only bucket. The relay
  itself logs counters only.
"""
from __future__ import annotations

import os

from aws_cdk import (Duration, RemovalPolicy, Stack, CfnOutput,
                     aws_certificatemanager as acm, aws_cloudwatch as cw, aws_cloudwatch_actions as cw_actions,
                     aws_ec2 as ec2, aws_ecs as ecs, aws_elasticloadbalancingv2 as elbv2, aws_iam as iam,
                     aws_logs as logs, aws_route53 as r53, aws_route53_targets as r53t, aws_s3 as s3,
                     aws_sns as sns, aws_sns_subscriptions as subs, aws_wafv2 as waf)
from constructs import Construct

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
RELAY_PORT = 7600


class RelayStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, domain: str, hosted_zone_id: str, zone_name: str,
                 relay_names: list[str], alarm_email: str | None = None, **kw):
        super().__init__(scope, cid, **kw)
        if not domain or not hosted_zone_id or not zone_name:
            raise ValueError("context domain, hostedZoneId and zoneName are required")
        if not relay_names or len(set(relay_names)) != len(relay_names):
            raise ValueError("relays must be a non-empty list of distinct names")

        # ---------------------------------------------------------------- network
        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=0, subnet_configuration=[
            ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24,
                                    map_public_ip_on_launch=False),
            ec2.SubnetConfiguration(name="relays", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
        ])
        vpc.add_flow_log("FlowLogs", destination=ec2.FlowLogDestination.to_cloud_watch_logs(
            logs.LogGroup(self, "FlowLogGroup", retention=logs.RetentionDays.ONE_MONTH)))
        isolated = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)
        vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3, subnets=[isolated])
        for name, svc in (("EcrApi", ec2.InterfaceVpcEndpointAwsService.ECR),
                          ("EcrDkr", ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER),
                          ("Logs", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS)):
            vpc.add_interface_endpoint(name, service=svc, subnets=isolated, private_dns_enabled=True)

        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=vpc, allow_all_outbound=False,
                                   description="Mycel relays: public TLS only")
        alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "wss from anywhere")
        task_sg = ec2.SecurityGroup(self, "TaskSg", vpc=vpc, allow_all_outbound=False,
                                    description="Mycel relay tasks: from the ALB only")
        task_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(RELAY_PORT), "relay port from ALB")
        alb_sg.add_egress_rule(task_sg, ec2.Port.tcp(RELAY_PORT), "to relay tasks")
        # Egress 443 only. The isolated subnets have no internet route, so this reaches only the VPC
        # endpoints (ECR, logs) and the S3 gateway endpoint (image layers).
        task_sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "VPC endpoints and S3 gateway only")

        # ---------------------------------------------------------------- TLS + DNS
        zone = r53.HostedZone.from_hosted_zone_attributes(self, "Zone", hosted_zone_id=hosted_zone_id, zone_name=zone_name)
        hosts = {n: f"{n}.{domain}" for n in relay_names}
        cert = acm.Certificate(self, "Cert", domain_name=f"*.{domain}",
                               validation=acm.CertificateValidation.from_dns(zone))

        logs_bucket = s3.Bucket(self, "AlbLogs", block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                                encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True, versioned=True,
                                object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
                                lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30),
                                                                  noncurrent_version_expiration=Duration.days(1))],
                                removal_policy=RemovalPolicy.RETAIN)

        alb = elbv2.ApplicationLoadBalancer(self, "Alb", vpc=vpc, internet_facing=True, security_group=alb_sg,
                                            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
                                            idle_timeout=Duration.seconds(120), drop_invalid_header_fields=True,
                                            deletion_protection=True)
        alb.log_access_logs(logs_bucket, prefix="alb")
        listener = alb.add_listener("Https", port=443, protocol=elbv2.ApplicationProtocol.HTTPS,
                                    certificates=[cert], ssl_policy=elbv2.SslPolicy.TLS13_13, open=False,
                                    default_action=elbv2.ListenerAction.fixed_response(404, content_type="text/plain",
                                                                                       message_body="not found"))

        # ---------------------------------------------------------------- relays
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.ENABLED)
        image = ecs.ContainerImage.from_asset(REPO_ROOT, file="deploy/relay/Dockerfile",
                                              platform=cdk_platform_arm64())
        log_group = logs.LogGroup(self, "RelayLogs", retention=logs.RetentionDays.ONE_MONTH)
        topic = sns.Topic(self, "Alarms")
        if alarm_email:
            topic.add_subscription(subs.EmailSubscription(alarm_email))

        for prio, (name, host) in enumerate(hosts.items(), start=1):
            task = ecs.FargateTaskDefinition(self, f"Task-{name}", cpu=512, memory_limit_mib=1024,
                                             runtime_platform=ecs.RuntimePlatform(
                                                 cpu_architecture=ecs.CpuArchitecture.ARM64,
                                                 operating_system_family=ecs.OperatingSystemFamily.LINUX))
            task.add_container("relay", image=image, readonly_root_filesystem=True, user="10001",
                               port_mappings=[ecs.PortMapping(container_port=RELAY_PORT)],
                               environment={"MYCEL_RELAY_ID": f"{self.region}-{name}", "MYCEL_RELAY_REGION": self.region},
                               logging=ecs.LogDrivers.aws_logs(stream_prefix=name, log_group=log_group),
                               stop_timeout=Duration.seconds(30))
            service = ecs.FargateService(self, f"Service-{name}", cluster=cluster, task_definition=task,
                                         desired_count=1, min_healthy_percent=0, max_healthy_percent=100,
                                         assign_public_ip=False, security_groups=[task_sg], vpc_subnets=isolated,
                                         circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
                                         enable_execute_command=False)
            tg = elbv2.ApplicationTargetGroup(self, f"Tg-{name}", vpc=vpc, port=RELAY_PORT,
                                              protocol=elbv2.ApplicationProtocol.HTTP, targets=[service],
                                              deregistration_delay=Duration.seconds(30),
                                              health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200",
                                                                             interval=Duration.seconds(10),
                                                                             healthy_threshold_count=2,
                                                                             unhealthy_threshold_count=2))
            listener.add_action(f"Route-{name}", priority=prio, action=elbv2.ListenerAction.forward([tg]),
                                conditions=[elbv2.ListenerCondition.host_headers([host]),
                                            elbv2.ListenerCondition.path_patterns(["/ws", "/status"])])
            r53.ARecord(self, f"Dns-{name}", zone=zone, record_name=host,
                        target=r53.RecordTarget.from_alias(r53t.LoadBalancerTarget(alb)))
            alarm = cw.Alarm(self, f"Unhealthy-{name}", metric=tg.metrics.unhealthy_host_count(period=Duration.minutes(1)),
                             threshold=0, comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                             evaluation_periods=3, treat_missing_data=cw.TreatMissingData.BREACHING,
                             alarm_description=f"relay {host} has no healthy task")
            alarm.add_alarm_action(cw_actions.SnsAction(topic))
            CfnOutput(self, f"Url-{name}", value=f"wss://{host}/ws")

        # ---------------------------------------------------------------- WAF
        acl = waf.CfnWebACL(self, "Waf", scope="REGIONAL", default_action={"allow": {}},
                            visibility_config=_vis("mycel-relays"), rules=[
            {"name": "ip-reputation", "priority": 0, "overrideAction": {"none": {}},
             "statement": {"managedRuleGroupStatement": {"vendorName": "AWS", "name": "AWSManagedRulesAmazonIpReputationList"}},
             "visibilityConfig": _vis("ip-reputation")},
            # One WebSocket = one request, so this caps reconnect storms and connection floods per address
            # while leaving room for many nodes behind one carrier-grade NAT.
            {"name": "connect-rate", "priority": 1, "action": {"block": {}},
             "statement": {"rateBasedStatement": {"limit": 600, "aggregateKeyType": "IP"}},
             "visibilityConfig": _vis("connect-rate")},
        ])
        waf.CfnWebACLAssociation(self, "WafAlb", resource_arn=alb.load_balancer_arn, web_acl_arn=acl.attr_arn)

        five_xx = cw.Alarm(self, "Elb5xx", metric=alb.metrics.http_code_elb(elbv2.HttpCodeElb.ELB_5XX_COUNT,
                                                                             period=Duration.minutes(5)),
                           threshold=100, evaluation_periods=2, treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                           alarm_description="load balancer 5xx (relays down or full)")
        five_xx.add_alarm_action(cw_actions.SnsAction(topic))


def cdk_platform_arm64():
    from aws_cdk.aws_ecr_assets import Platform
    return Platform.LINUX_ARM64


def _vis(name: str) -> dict:
    return {"cloudWatchMetricsEnabled": True, "metricName": name, "sampledRequestsEnabled": True}
