#!/usr/bin/env python3
"""Generate a cryptographically strong random secret.

By default the secret is NOT printed — it's copied to your clipboard so it
never lands in terminal scrollback or shell history. Use --show only when you
deliberately need it on stdout (e.g. to pipe it straight into another command).
"""

import sys
import argparse
import secrets
import string
import subprocess

ALPHABET = string.ascii_letters + string.digits + string.punctuation


def generate(length):
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def copy_to_clipboard(value):
    """Try the common clipboard tools; return the one that worked, or None."""
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["clip"]):
        try:
            subprocess.run(cmd, input=value, text=True, check=True)
            return cmd[0]
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return None


def main(argv):
    parser = argparse.ArgumentParser(description="Generate a strong random secret.")
    parser.add_argument("-l", "--length", type=int, default=16,
                        help="length of the secret (default: 16)")
    parser.add_argument("--show", action="store_true",
                        help="print the secret to stdout instead of copying "
                             "(use only when piping it into another command)")
    args = parser.parse_args(argv)

    if args.length < 1:
        parser.error("--length must be at least 1")

    secret = generate(args.length)

    if args.show:
        print(secret)
        return 0

    tool = copy_to_clipboard(secret)
    if tool:
        print(f"Secret ({args.length} chars) copied to clipboard via {tool}. "
              "It was not printed; paste it where you need it.")
        return 0

    print("No clipboard tool found (tried pbcopy, xclip, clip).", file=sys.stderr)
    print("Install one, or re-run with --show to print the secret.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
