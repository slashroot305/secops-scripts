# sysops-scripts

**Protect a fleet of AWS machines with CrowdStrike Falcon — the way a real security team does it, not by hand.**

If you run servers on AWS, every one of them needs endpoint protection. Doing that by SSH-ing into each box and pasting API keys around doesn't scale and isn't safe. This repo walks you through the alternative: store your credentials once in AWS Secrets Manager, then push the Falcon sensor to your whole fleet with a single command — with the secrets never leaving AWS.

It's built to be read start to finish by someone who's never done it before. Follow the chapters in order and you'll have endpoint protection rolled out across an AWS account by the end.

---

## The story in one picture

```
  Chapter 1                Chapter 2                 Chapter 3
  ─────────                ─────────                 ─────────
  Set up access       Store the Falcon keys      Push Falcon to the fleet
  (IAM user, least    (Secrets Manager,          (one command → SSM runs
   privilege)          rotate on a schedule)       the installer everywhere)
      │                       │                           │
      └───────────────────────┴───────────────────────────┘
                              │
                        Chapter 4
                        ─────────
                   Watch what it costs
                   (multi-account Cost Explorer)
```

Each chapter is a script in this repo. Nothing is magic — you can read every line.

---

## Chapter 1 — Set up least-privilege access

Before anything touches your account, create a dedicated IAM user for this work instead of using root or your everyday admin.

```bash
# Generate a strong initial secret first
python3 python/password_generator.py

# Create the user
python3 aws/create_user_aws.py falcon-deployer
```

Give that user only what Chapters 2–4 need: `secretsmanager:*` on your Falcon secret, `ssm:SendCommand` / `ssm:ListCommand*`, `ec2:DescribeInstances`, and Cost Explorer read. When you're done experimenting, clean up:

```bash
python3 aws/delete_user.py
```

> Files: [aws/create_user_aws.py](aws/create_user_aws.py), [aws/delete_user.py](aws/delete_user.py), [python/password_generator.py](python/password_generator.py)

---

## Chapter 2 — Store the Falcon API keys (and rotate them)

The Falcon installers need a CrowdStrike API **client ID** and **client secret** (generate them at <https://falcon.crowdstrike.com/api-clients-and-keys>). The wrong way is to paste those into a shell or commit them. The right way is one secret in AWS Secrets Manager that every instance reads for itself.

### Store it

Put the credentials in a single JSON secret. Pick a name you'll reuse in Chapter 3 — e.g. `crowdstrike/falcon-api`:

```bash
aws secretsmanager create-secret \
  --name crowdstrike/falcon-api \
  --description "CrowdStrike Falcon API credentials for sensor install" \
  --secret-string '{
    "client_id": "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET",
    "base_url": "https://api.us-2.crowdstrike.com"
  }'
```

> `base_url` is your Falcon cloud's API host — `us-1`, `us-2`, `eu-1`, etc. Leave it as shown if you're on US-2.

Verify it reads back as valid JSON (you should see your keys):

```bash
aws secretsmanager get-secret-value \
  --secret-id crowdstrike/falcon-api \
  --query SecretString --output text
```

### Let the instances read it (least privilege)

Each EC2 instance reads this one secret through its instance role. Attach a policy scoped to *just* this secret — never `Resource: "*"`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "secretsmanager:GetSecretValue",
    "Resource": "arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:crowdstrike/falcon-api-*"
  }]
}
```

(The trailing `-*` matches the random suffix Secrets Manager appends to the ARN.)

### Rotate it

API keys age out and leak. Rotating means replacing the stored value so the next deploy picks up fresh keys automatically — no code changes.

**Manual rotation (do this today, takes a minute):**

1. In the Falcon console, create a new API client and copy its ID/secret.
2. Store the new values as a new version of the *same* secret:
   ```bash
   aws secretsmanager put-secret-value \
     --secret-id crowdstrike/falcon-api \
     --secret-string '{
       "client_id": "NEW_CLIENT_ID",
       "client_secret": "NEW_CLIENT_SECRET",
       "base_url": "https://api.us-2.crowdstrike.com"
     }'
   ```
   Secrets Manager labels this new version `AWSCURRENT` and keeps the old one as `AWSPREVIOUS`, so you can roll back with one command if the new keys are bad.
3. Delete the old API client in the Falcon console once the next deploy succeeds.

Nothing in Chapter 3 changes — the fleet reads `AWSCURRENT` on the next run and gets the new keys.

**Automatic rotation (optional, later):** Secrets Manager can call a Lambda on a schedule to do the above for you. Set it up with `aws secretsmanager rotate-secret --secret-id crowdstrike/falcon-api --rotation-lambda-arn <ARN> --rotation-rules AutomaticallyAfterDays=90`. Writing that Lambda is its own project; the manual flow above is the right place to start.

---

## Chapter 3 — Push Falcon to the fleet (the keystone)

This is where CrowdStrike and AWS meet. [`deploy_falcon_fleet.py`](crowdstrike-falcon/deploy_falcon_fleet.py) hands the install off to AWS Systems Manager (SSM) Run Command. SSM runs the right installer on every targeted instance at once, and **each instance pulls the credentials from Secrets Manager itself** — so the secret never passes through this script, the Run Command parameters, or CloudTrail.

```
  deploy_falcon_fleet.py ──SendCommand──▶ SSM ──runs on──▶ each EC2 instance
                                                                │
                                      (1) reads creds from Secrets Manager
                                      (2) downloads the matching installer
                                      (3) runs it with the creds in its env
```

### Prerequisites

- Target instances are **SSM-managed nodes**: the SSM Agent is running and the instance profile includes `AmazonSSMManagedInstanceCore`.
- Those instance roles have the secret-read policy from Chapter 2.
- `pip install boto3` on the machine running the script.

### See exactly what will run (no AWS needed)

```bash
python3 crowdstrike-falcon/deploy_falcon_fleet.py \
  --platform linux --secret-id crowdstrike/falcon-api \
  --tag Environment=prod --region us-east-1 --dry-run
```

`--dry-run` prints the exact commands each instance would execute and sends nothing.

### Deploy for real

```bash
# All Linux instances tagged Environment=prod
python3 crowdstrike-falcon/deploy_falcon_fleet.py \
  --platform linux --secret-id crowdstrike/falcon-api \
  --tag Environment=prod --region us-east-1

# Specific Windows instances
python3 crowdstrike-falcon/deploy_falcon_fleet.py \
  --platform windows --secret-id crowdstrike/falcon-api \
  --instance-ids i-0abc123 i-0def456 --region us-east-1
```

You get a `PASS`/`FAIL` line per instance, with the failing output inline so you can fix and re-run.

### What actually installs the sensor

The orchestrator doesn't reinvent the install — it runs the same per-OS scripts you can also run by hand on a single box:

| Platform | Installer |
|---|---|
| Ubuntu (x86_64 / arm64) | [falcon-install-ubuntu.sh](crowdstrike-falcon/falcon-install-ubuntu.sh) |
| macOS (universal) | [falcon-install-macos.sh](crowdstrike-falcon/falcon-install-macos.sh) |
| Windows | [falcon-install-windows.ps1](crowdstrike-falcon/falcon-install-windows.ps1) |

Each authenticates to the Falcon API, downloads the N-1 sensor, validates its SHA256, installs it, registers the CCID, and runs a post-install health check. To install on one machine directly:

```bash
export CS_CLIENT_ID=your_client_id
export CS_CLIENT_SECRET=your_client_secret
sudo bash crowdstrike-falcon/falcon-install-ubuntu.sh
```

---

## Chapter 4 — Watch what it costs

Endpoint protection across a fleet means instances, SSM, and Secrets Manager all showing up on the bill. [`aws_cost_analyzer.py`](aws/aws_cost_analyzer.py) pulls Cost Explorer data across every account in your Organization, breaks spend down by service, and flags the top drivers.

```bash
pip install boto3 python-dateutil
python3 aws/aws_cost_analyzer.py --profile my-profile --email me@example.com --budget 100
```

> File: [aws/aws_cost_analyzer.py](aws/aws_cost_analyzer.py)

---

## Repository layout

```
sysops-scripts/
├── aws/
│   ├── create_user_aws.py      # Chapter 1 — create the IAM user
│   ├── delete_user.py          # Chapter 1 — tear it down
│   └── aws_cost_analyzer.py    # Chapter 4 — multi-account cost breakdown
├── crowdstrike-falcon/
│   ├── deploy_falcon_fleet.py  # Chapter 3 — THE KEYSTONE: push to the fleet via SSM
│   ├── falcon-install-ubuntu.sh
│   ├── falcon-install-macos.sh
│   └── falcon-install-windows.ps1
└── python/
    └── password_generator.py   # Chapter 1 — strong initial secret
```

---

## Requirements

- **Python** 3.8+ — `boto3` (and `python-dateutil` for the cost analyzer)
- **AWS CLI** configured (`aws configure`) with access to SSM, Secrets Manager, EC2, and Cost Explorer
- **CrowdStrike** API credentials — generate at <https://falcon.crowdstrike.com/api-clients-and-keys>
- Target EC2 instances must be **SSM-managed nodes**

---

## Security considerations

- Credentials live in **one** Secrets Manager secret and are read on-instance — never commit them, never pass them as command arguments.
- Scope every IAM policy to the specific secret ARN and the specific SSM actions. No `Resource: "*"`.
- Rotate the Falcon API keys (Chapter 2). Keep `AWSPREVIOUS` around long enough to roll back a bad rotation.
- Always run `--dry-run` first against a new target set.

---

## License

MIT — see [LICENSE](LICENSE)
