#!/usr/bin/env python3
"""
Deploy CrowdStrike Falcon across an AWS EC2 fleet
───────────────────────────────────────────────────────────────
This is the bridge between the two halves of this repo: the per-OS
Falcon installers (falcon-install-*.sh / .ps1) and your AWS account.

Instead of SSH-ing into every box and pasting API keys into a shell,
this script hands the install off to AWS Systems Manager (SSM) Run
Command. SSM runs the installer on every targeted instance at once,
and each instance pulls the CrowdStrike API credentials from AWS
Secrets Manager *itself* — so the secrets never travel through this
script, never land in the Run Command parameters, and never show up
in CloudTrail. That's the "real security team" way to do it.

How it fits together:

    this script ──SendCommand──▶ SSM ──runs on──▶ EC2 instance
                                                      │
                          (1) reads creds from Secrets Manager
                          (2) downloads the matching installer
                          (3) runs it with the creds in its env

What you need first (see the README walkthrough):
  • EC2 instances that are SSM-managed nodes (SSM Agent running +
    an instance profile with AmazonSSMManagedInstanceCore).
  • Each instance's role allowed secretsmanager:GetSecretValue on
    the ONE secret below (least privilege).
  • A secret storing the Falcon API creds as JSON, e.g.:
        {
          "client_id": "...",
          "client_secret": "...",
          "base_url": "https://api.us-2.crowdstrike.com"
        }

Requirements:
  pip install boto3

Usage:
  # Target instances by tag (recommended). Validate on a non-prod tag
  # like Environment=staging before you ever point this at prod.
  python3 deploy_falcon_fleet.py --platform linux \\
      --secret-id crowdstrike/falcon-api \\
      --tag Environment=staging --region us-east-1

  # Target specific instance IDs
  python3 deploy_falcon_fleet.py --platform windows \\
      --secret-id crowdstrike/falcon-api \\
      --instance-ids i-0abc123 i-0def456 --region us-east-1

  # See exactly what would run, send nothing
  python3 deploy_falcon_fleet.py --platform linux \\
      --secret-id crowdstrike/falcon-api --tag Role=web --dry-run
"""

import sys
import time
import argparse

# Raw installer URLs. Point these at your fork/branch, or override with
# --script-url to serve from a private S3 presigned URL instead.
REPO_RAW_BASE = (
    "https://raw.githubusercontent.com/slashroot305/secops-scripts/"
    "main/crowdstrike-falcon"
)
DEFAULT_SCRIPT_URLS = {
    "linux":   f"{REPO_RAW_BASE}/falcon-install-ubuntu.sh",
    "windows": f"{REPO_RAW_BASE}/falcon-install-windows.ps1",
}

# SSM document per platform. (macOS instances are also SSM-managed and
# use AWS-RunShellScript, but EC2 Mac is niche, so we leave it opt-in.)
SSM_DOCUMENTS = {
    "linux":   "AWS-RunShellScript",
    "windows": "AWS-RunPowerShellScript",
}


# ── On-instance bootstrap: fetch creds, download installer, run it ──────
def build_linux_commands(secret_id, region, script_url):
    """Shell run ON each Linux box by SSM. Secrets are read here, on-box."""
    return [
        "set -euo pipefail",
        'command -v jq >/dev/null 2>&1 || (apt-get update -y && apt-get install -y jq)',
        f'SECRET=$(aws secretsmanager get-secret-value --secret-id "{secret_id}" '
        f'--region "{region}" --query SecretString --output text)',
        'export CS_CLIENT_ID=$(echo "$SECRET" | jq -r .client_id)',
        'export CS_CLIENT_SECRET=$(echo "$SECRET" | jq -r .client_secret)',
        'export CS_BASE_URL=$(echo "$SECRET" | jq -r ".base_url // empty")',
        f'curl -fsSL "{script_url}" -o /tmp/falcon-install.sh',
        "bash /tmp/falcon-install.sh",
        "rm -f /tmp/falcon-install.sh",
    ]


def build_windows_commands(secret_id, region, script_url):
    """PowerShell run ON each Windows box by SSM. Secrets are read here, on-box."""
    return [
        "$ErrorActionPreference = 'Stop'",
        f'$json = aws secretsmanager get-secret-value --secret-id "{secret_id}" '
        f'--region "{region}" --query SecretString --output text',
        "$secret = $json | ConvertFrom-Json",
        "$env:CS_CLIENT_ID = $secret.client_id",
        "$env:CS_CLIENT_SECRET = $secret.client_secret",
        "if ($secret.base_url) { $env:CS_BASE_URL = $secret.base_url }",
        f'Invoke-WebRequest -Uri "{script_url}" -OutFile "$env:TEMP\\falcon-install.ps1"',
        'powershell -ExecutionPolicy Bypass -File "$env:TEMP\\falcon-install.ps1"',
        'Remove-Item "$env:TEMP\\falcon-install.ps1" -Force',
    ]


# ── Targeting ───────────────────────────────────────────────────────────
def build_targets(instance_ids, tag):
    """SSM targets: explicit instance IDs, or a single Key=Value tag filter."""
    if instance_ids:
        return [{"Key": "InstanceIds", "Values": instance_ids}]
    key, _, value = tag.partition("=")
    return [{"Key": f"tag:{key}", "Values": [value]}]


# ── Status polling ───────────────────────────────────────────────────────
def wait_for_command(ssm, command_id, poll_seconds=5, timeout_seconds=600):
    """Poll the overall command until it leaves an in-progress state."""
    terminal = {"Success", "Cancelled", "Failed", "TimedOut"}
    waited = 0
    while True:
        resp = ssm.list_commands(CommandId=command_id)["Commands"][0]
        status = resp["Status"]
        if status in terminal or status == "Cancelling":
            return status
        if waited >= timeout_seconds:
            return f"TIMED_OUT_LOCALLY (last status: {status})"
        time.sleep(poll_seconds)
        waited += poll_seconds


def report_per_instance(ssm, command_id):
    """Print a PASS/FAIL line per instance, with the failing output if any."""
    invocations = ssm.list_command_invocations(
        CommandId=command_id, Details=True
    )["CommandInvocations"]

    if not invocations:
        print("  (no instances matched the target — nothing to report)")
        return 1

    failures = 0
    for inv in invocations:
        instance = inv["InstanceId"]
        status = inv["Status"]
        mark = "PASS" if status == "Success" else "FAIL"
        if status != "Success":
            failures += 1
        print(f"  [{mark}] {instance:<20} {status}")
        if status != "Success":
            for plugin in inv.get("CommandPlugins", []):
                out = (plugin.get("Output") or "").strip()
                if out:
                    tail = "\n".join(out.splitlines()[-8:])
                    print(f"         └─ {tail.replace(chr(10), chr(10) + '            ')}")
    return failures


# ── Main ─────────────────────────────────────────────────────────────────
def deploy(args):
    script_url = args.script_url or DEFAULT_SCRIPT_URLS[args.platform]
    if args.platform == "linux":
        commands = build_linux_commands(args.secret_id, args.region, script_url)
    else:
        commands = build_windows_commands(args.secret_id, args.region, script_url)

    targets = build_targets(args.instance_ids, args.tag)
    document = SSM_DOCUMENTS[args.platform]

    print(f"Platform : {args.platform}  ({document})")
    print(f"Region   : {args.region}")
    print(f"Secret   : {args.secret_id}  (read on-instance, never by this script)")
    print(f"Installer: {script_url}")
    print(f"Targets  : {targets}")

    if args.dry_run:
        print("\n--dry-run: the following would run on each instance:\n")
        print("\n".join(f"    {line}" for line in commands))
        return 0

    # Imported here so --dry-run works with no boto3 install and no AWS creds.
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ssm = session.client("ssm")

    try:
        resp = ssm.send_command(
            Targets=targets,
            DocumentName=document,
            Parameters={"commands": commands},
            Comment="Install CrowdStrike Falcon sensor",
            TimeoutSeconds=600,
        )
    except (ClientError, BotoCoreError) as e:
        print(f"\nFailed to send command: {e}", file=sys.stderr)
        return 2

    command_id = resp["Command"]["CommandId"]
    print(f"\nSent SSM command {command_id}. Waiting for instances...\n")

    # Brief pause so invocations register before we poll.
    time.sleep(3)
    overall = wait_for_command(ssm, command_id)
    print(f"Overall status: {overall}\n")

    failures = report_per_instance(ssm, command_id)
    if failures:
        print(f"\n{failures} instance(s) failed. See output above.")
        return 1
    print("\nAll targeted instances installed the Falcon sensor successfully.")
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Deploy the CrowdStrike Falcon sensor across an AWS EC2 fleet via SSM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--platform", required=True, choices=["linux", "windows"],
                   help="OS family of the target instances")
    p.add_argument("--secret-id", required=True,
                   help="Secrets Manager secret holding the Falcon API creds (JSON)")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--instance-ids", nargs="+", metavar="ID",
                        help="One or more EC2 instance IDs")
    target.add_argument("--tag", metavar="Key=Value",
                        help="Target all instances matching this tag")
    p.add_argument("--region", required=True, help="AWS region")
    p.add_argument("--profile", help="AWS named profile (optional)")
    p.add_argument("--script-url",
                   help="Override the installer URL (e.g. a private S3 presigned URL)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would run on each instance and exit")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    sys.exit(deploy(args))
