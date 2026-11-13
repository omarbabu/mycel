# Running Mycel relays

A relay is a small, stateless forwarder. Nodes keep an **outbound** WebSocket to two or three
relays; a relay passes end-to-end-encrypted frames between nodes by node id. It never sees
plaintext, stores nothing on disk, and logs only counters. Relays are also where a brand-new
node finds its first peers.

Anyone can run one. A fresh install needs at least one in `mycel/seeds.json`. After its first
join a node also remembers relays it learns from peers, so the network doesn't depend on the
seed relays after day one.

## What a relay exposes

| Path | Who | What |
|---|---|---|
| `/ws` | any node | WebSocket. Admission requires a signature from the node's key and a proof-of-work identity ticket. |
| `/status` | anyone | Public counters (connections, frames, bytes, version). No node ids, no addresses. |
| `/health` | load balancer only | 200, or 503 while draining. Not routed publicly. |

Built-in limits (`mycel/relay.py`, `RelayLimits`):
- **Per connection:** byte and frame token buckets, a bounded send queue, and a 2 MB maximum frame.
- **Unauthenticated handshakes:** capped for the whole relay and per source address.
- **New handshakes:** rate-limited per address.
- **Connections:** a per-address connection cap, set high for carrier-grade NAT.

On SIGTERM the relay fails its health check, waits for the balancer to stop routing to it, and
then closes every connection with "going away". Nodes heal onto their other relays.

## Reference deployment on AWS (`cdk/`)

```
internet ──443 (TLS 1.3)──> ALB + WAF ──host rule──> one Fargate task per relay
                                                     (isolated subnets, no public IP,
                                                      no route to the internet)
```

- **One URL per relay process.** Two nodes on the same relay URL must land in the same
  process, so each relay is its own single-task service and its own host name (`a.relay.example.org`,
  `b.relay.example.org`). Deployments never run two tasks for one relay.
- **Only 443, only on the load balancer.** Tasks accept the relay port from the balancer's
  security group and nothing else. There's no port 80 listener.
- **No egress.** Tasks sit in isolated subnets; VPC endpoints serve image pulls and logs.
- **WAF.** The AWS IP-reputation list plus a per-address rate limit on connection attempts.
- **Logs.** The relay itself logs counters only. ALB access logs (which include client IPs) go to
  a private, TLS-only, versioned bucket and expire after 30 days. Shorten or disable this to
  match your privacy policy. VPC flow logs are kept for 30 days.
- **Alarms.** An alarm for a relay with no healthy task, and one for ALB 5xx, both sent to SNS
  (email optional).

### Deploy

```bash
cd deploy/relay/cdk
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cdk bootstrap                         # once per account/region
cdk deploy -c domain=relay.example.org -c hostedZoneId=Z123... -c zoneName=example.org \
           -c relays='["a","b"]' -c region=eu-west-1 -c alarmEmail=ops@example.org \
           -c stackName=MycelRelays-euw1
```

You need a Route 53 hosted zone for the domain. The wildcard certificate is issued by ACM and
validated through that zone. Deploy one stack per region.

### Verify before announcing a relay

```bash
curl -s https://a.relay.example.org/status          # 200 with counters, no ids or IPs
curl -s -o /dev/null -w '%{http_code}\n' https://a.relay.example.org/health   # 404 (not routed publicly)
curl -s -o /dev/null -w '%{http_code}\n' https://relay.example.org/            # 404 (default action)
openssl s_client -connect a.relay.example.org:443 -tls1_2 </dev/null   # must fail (TLS 1.3 only)
mycel start --relay wss://a.relay.example.org/ws    # on two machines on different networks: join, then chat
```

Then confirm in the AWS console that the tasks have no public IP and that the task security group
admits only the ALB's security group.

### Publish it

Add the URLs to `mycel/seeds.json` in a release. Use 2–3 relays across at least two regions.
Users can also add relays themselves in Settings → Network, or with `mycel start --relay URL`.

## Cost (rough, us-east-1, two relays)

| Item | Per month |
|---|---|
| ALB (hours + LCUs at modest load) | ~$25 |
| Fargate ARM, 0.5 vCPU / 1 GB × 2 | ~$29 |
| Interface endpoints (ECR ×2, Logs) × 2 AZs | ~$44 |
| WAF (ACL + 2 rules + requests) | ~$8 |
| **Data out** | **$0.09/GB.** This dominates at scale: swarm activations cross the relay. |

Traffic inbound to the relay is free. Every byte a relay forwards is billed once, as data out.

## Operating

- **Abuse.** Relays can't read traffic. They can only rate-limit, refuse connections, or (with
  an operator block list, not yet built) refuse specific node ids. Publish an abuse contact with
  the relay list.
- **Capacity.** One 0.5 vCPU task handles thousands of mostly idle connections. Scale by adding
  relays (new host names), not tasks.
- **Upgrades.** Relays and nodes negotiate `proto` in the challenge. Keep old relays running until
  nodes have upgraded.
