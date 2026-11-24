# secops-scripts

**A small, growing toolkit for running CrowdStrike Falcon on AWS — built security-minded, in the order you'd actually do it.**

This repo is a work in progress, and that's the point: it grows one real, working script at a time. Everything documented here exists and runs today — no placeholders, no "coming soon." As the toolkit expands, so does this README.

The thread tying it together: protect a fleet of AWS machines with CrowdStrike Falcon, the way a real security team does it. That means controlling access first, deploying protection safely, and keeping an eye on what it costs.

> **This assumes you already have an AWS + CrowdStrike environment.** No environment of your own? Read on anyway — every step has a **Why** explaining the security reasoning, and the dry-run output below shows exactly what happens. The *thinking* is the part that travels with you, with or without an account to run it in.

---

## What's here today

Three things, in the order a security-minded person would reach for them:

1. **Set up least-privilege access** — a dedicated IAM user to do this work, not root.
2. **Deploy Falcon to your EC2 fleet** — push the sensor to every box via SSM, with credentials stored in Secrets Manager (never in a shell).
3. **Watch what it costs** — a multi-account cost breakdown so the bill never surprises you.

---

## 1. Set up least-privilege access

Before touching anything, create a dedicated IAM user instead of using root or your everyday admin. Give it only what the later steps need and nothing more.

```bash
# Create the user
python3 aws/create_user_aws.py falcon-deployer

# ...and remove it when you're done
python3 aws/delete_user.py
```

`create_user_aws.py` creates the IAM user only — it doesn't set a console password or access keys. When you do set those up, generate the secret with:

```bash
# Copies a strong secret to your clipboard — nothing is printed to the terminal
python3 passgen/password_generator.py

# Use --show only when you need to pipe it straight into another command
python3 passgen/password_generator.py --show -l 24
```

Scope that user to just: `secretsmanager:GetSecretValue` on your Falcon secret, `ssm:SendCommand` / `ssm:ListCommand*`, `ec2:DescribeInstances`, and Cost Explorer read.

> **Why a dedicated user at all?** Root (and your everyday admin) can do *anything* — so if those credentials leak, the attacker can too. A purpose-built user that can only deploy Falcon means a leak costs you a Falcon deploy, not your whole account. The instinct to carry anywhere: give each task its own identity with the fewest powers that still get the job done.
>
> **Why generate the secret to the clipboard, not the screen?** Anything printed to a terminal lives on in scrollback and shell history — a plaintext password sitting in `~/.zsh_history` is a credential you've already lost. Keeping it off the screen is the cheapest security win there is.

> Files: [aws/create_user_aws.py](aws/create_user_aws.py) · [aws/delete_user.py](aws/delete_user.py) · [passgen/password_generator.py](passgen/password_generator.py)

---

## 2. Deploy Falcon to your EC2 fleet

[`deploy_falcon_fleet.py`](crowdstrike-falcon/deploy_falcon_fleet.py) is the bridge between CrowdStrike and AWS. It hands the install to AWS Systems Manager (SSM) Run Command, which runs the right installer on every targeted instance at once. The security-minded detail: **each instance pulls the API credentials from Secrets Manager itself** — so the secret never passes through this script, the Run Command parameters, or CloudTrail.

```
  deploy_falcon_fleet.py ──SendCommand──▶ SSM ──runs on──▶ each EC2 instance
                                                                │
                                      (1) reads creds from Secrets Manager
                                      (2) downloads the matching installer
                                      (3) runs it with the creds in its env
```

> **Why let each instance fetch the secret itself, instead of this script passing it in?** If the script read the keys and handed them to SSM, they'd show up in the Run Command parameters and in CloudTrail — a permanent, auditable record of your secret in plaintext. By having the instance fetch its own copy at run time, the secret only ever exists in that instance's memory for a moment. The deploy tool never sees it, so it can never leak it. This is the whole reason the deploy is built the way it is.

### First, store the Falcon API keys

Generate API credentials at <https://falcon.crowdstrike.com/api-clients-and-keys>, then store them as one JSON secret — never paste them into a shell or commit them:

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

> **Why a secrets manager instead of an env var or a file?** Secrets in files get committed by accident; secrets in env vars leak into logs and crash dumps. A secrets manager gives you one guarded place with an audit trail of who read it and when — and, crucially, lets you rotate the value without touching any code that uses it.

Scope each instance's role to *just* this secret — never `Resource: "*"`:

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

> **Why scope the ARN so tightly?** If these keys ever leak, a narrow policy means the blast radius is this one secret — not every secret in the account. "Least privilege" isn't a slogan; it's literally *how small you can make that `Resource` line*. Same instinct as the dedicated user above, applied to data instead of identity.

To rotate later, `put-secret-value` a new version onto the same secret — the new value becomes `AWSCURRENT`, the old stays `AWSPREVIOUS` so you can roll back, and the next deploy picks it up with no code change.

> **Why rotation matters:** credentials are perishable. Keys leak, people leave, tokens age out. Rotation that needs zero code changes is rotation you'll actually do — and the `AWSPREVIOUS` safety net means a bad key swap is a one-command undo, not an outage.

### Preview, then deploy

```bash
# See exactly what would run on each box — no AWS needed
python3 crowdstrike-falcon/deploy_falcon_fleet.py \
  --platform linux --secret-id crowdstrike/falcon-api \
  --tag Environment=staging --region us-east-1 --dry-run
```

**No AWS account? Run exactly that.** `--dry-run` needs no credentials and no `boto3` — it just prints the plan. This is the one command in the whole repo anyone can run, and it's a window into how the real thing works:

```
Platform : linux  (AWS-RunShellScript)
Region   : us-east-1
Secret   : crowdstrike/falcon-api  (read on-instance, never by this script)
Installer: https://raw.githubusercontent.com/slashroot305/secops-scripts/main/crowdstrike-falcon/falcon-install-ubuntu.sh
Targets  : [{'Key': 'tag:Environment', 'Values': ['staging']}]

--dry-run: the following would run on each instance:

    set -euo pipefail
    command -v jq >/dev/null 2>&1 || (apt-get update -y && apt-get install -y jq)
    SECRET=$(aws secretsmanager get-secret-value --secret-id "crowdstrike/falcon-api" --region "us-east-1" --query SecretString --output text)
    export CS_CLIENT_ID=$(echo "$SECRET" | jq -r .client_id)
    export CS_CLIENT_SECRET=$(echo "$SECRET" | jq -r .client_secret)
    export CS_BASE_URL=$(echo "$SECRET" | jq -r ".base_url // empty")
    curl -fsSL "https://raw.githubusercontent.com/slashroot305/secops-scripts/main/crowdstrike-falcon/falcon-install-ubuntu.sh" -o /tmp/falcon-install.sh
    bash /tmp/falcon-install.sh
    rm -f /tmp/falcon-install.sh
```

Read those lines top to bottom and you've learned the whole pattern: the instance installs `jq`, fetches the secret *itself*, exports the creds only into its own environment, downloads the installer, runs it, and cleans up. Notice the creds are never a command-line argument — that's the security lesson, visible right there in the plan.

Once you *do* have an environment, drop `--dry-run` to deploy for real — starting with a non-prod tag, then widening to prod once it's proven:

```bash
# Start here: all Linux instances tagged Environment=staging
python3 crowdstrike-falcon/deploy_falcon_fleet.py \
  --platform linux --secret-id crowdstrike/falcon-api \
  --tag Environment=staging --region us-east-1

# Only once staging looks good: the same command against Environment=prod
# python3 crowdstrike-falcon/deploy_falcon_fleet.py \
#   --platform linux --secret-id crowdstrike/falcon-api \
#   --tag Environment=prod --region us-east-1

# Specific Windows instances
python3 crowdstrike-falcon/deploy_falcon_fleet.py \
  --platform windows --secret-id crowdstrike/falcon-api \
  --instance-ids i-0abc123 i-0def456 --region us-east-1
```

You get a `PASS`/`FAIL` line per instance, with failing output inline so you can fix and re-run.

> **Why staging before prod?** A fleet command runs on every matching box at once — a wrong tag or region could hit machines you didn't mean to. Proving it on a throwaway `staging` tag first means a mistake costs you a test box, not production. Combined with `--dry-run`, that's two cheap habits that turn "oops, I just ran that on prod" into a non-event. Look before you leap, then leap somewhere safe first.

**Prerequisites:** target instances are SSM-managed nodes (SSM Agent running + `AmazonSSMManagedInstanceCore` on the instance profile), their roles carry the secret-read policy above, and `pip install boto3` where you run the script.

### The installers it runs

The orchestrator doesn't reinvent the install — it runs the same per-OS scripts you can also run by hand on a single box. Each authenticates to the Falcon API, downloads the N-1 sensor, validates its SHA256, installs it, registers the CCID, and runs a post-install health check.

| Platform | Installer |
|---|---|
| Ubuntu (x86_64 / arm64) | [falcon-install-ubuntu.sh](crowdstrike-falcon/falcon-install-ubuntu.sh) |
| macOS (universal) | [falcon-install-macos.sh](crowdstrike-falcon/falcon-install-macos.sh) |
| Windows | [falcon-install-windows.ps1](crowdstrike-falcon/falcon-install-windows.ps1) |

```bash
# Single-box install, by hand
export CS_CLIENT_ID=your_client_id
export CS_CLIENT_SECRET=your_client_secret
sudo bash crowdstrike-falcon/falcon-install-ubuntu.sh
```

> The fleet orchestrator currently targets `linux` and `windows`. The macOS installer is here for hand-installs (EC2 Mac is niche).

---

## 3. Watch what it costs

Running a fleet means EC2, SSM, and Secrets Manager all show up on the bill. [`aws_cost_analyzer.py`](aws/aws_cost_analyzer.py) pulls Cost Explorer data across every account in your AWS Organization, breaks spend down by service, and flags the top drivers.

```bash
pip install boto3 python-dateutil
python3 aws/aws_cost_analyzer.py --profile my-profile --email me@example.com --budget 100
```

> **Why does cost belong in a security repo?** Two reasons. A surprise bill is often the *first sign of a breach* — crypto-miners on your instances show up as a spend spike before anything else does. And security you can't afford to keep running isn't security. Watching the bill is part of watching the fleet.

> File: [aws/aws_cost_analyzer.py](aws/aws_cost_analyzer.py)

---

## Repository layout

```
secops-scripts/
├── aws/
│   ├── create_user_aws.py      # least-privilege IAM user
│   ├── delete_user.py          # tear it down
│   └── aws_cost_analyzer.py    # multi-account cost breakdown
├── crowdstrike-falcon/
│   ├── deploy_falcon_fleet.py  # push the sensor to the fleet via SSM
│   ├── falcon-install-ubuntu.sh
│   ├── falcon-install-macos.sh
│   └── falcon-install-windows.ps1
└── passgen/
    └── password_generator.py   # strong random secret
```

---

## Requirements

- **Python** 3.8+ — `boto3` (and `python-dateutil` for the cost analyzer)
- **AWS CLI** configured (`aws configure`) with access to SSM, Secrets Manager, EC2, and Cost Explorer
- **CrowdStrike** API credentials — generate at <https://falcon.crowdstrike.com/api-clients-and-keys>
- Target EC2 instances must be **SSM-managed nodes**

---

## Security considerations

- Create a dedicated least-privilege user before doing anything else — don't work as root.
- Generated secrets go to the clipboard, not the terminal — keep them out of scrollback and shell history (`--show` is an explicit opt-in).
- Credentials live in **one** Secrets Manager secret, read on-instance — never commit them, never pass them as command arguments.
- Scope every IAM policy to the specific secret ARN and specific actions. No `Resource: "*"`.
- Always `--dry-run` against a new target set first.

---

## License

MIT — see [LICENSE](LICENSE)
