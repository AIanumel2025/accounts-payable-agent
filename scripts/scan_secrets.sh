#!/bin/sh
# Scans directories for credential-shaped values (used on the Docker images' application files in CI).
# Precise on purpose: a DSN counts only when it carries a literal, non-placeholder password
# (no template braces/percent/dollar, not a documented placeholder word), so source comments,
# f-string URI construction and the fake OpenAPI-export DSN are not findings, while a real
# `user:secretvalue@host` still is. Other patterns (Clerk secret keys, PEM private keys, AWS-style
# access key ids) are unchanged. Exit 1 on any finding; matches are reported by file and line only.
set -u
pattern='sk_(live|test)_[A-Za-z0-9]{8,}|postgres(ql)?://[A-Za-z0-9_.~-]+:(?!(?i:password|passwd|pass|secret|unused|example|changeme|placeholder|redacted|x{3,})(@|[^A-Za-z0-9]))[^\s"'"'"'@{}<>$%()/\\]{4,}@[A-Za-z0-9.-]+|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|AKIA[0-9A-Z]{12,}'
status=0
for root in "$@"; do
  [ -d "$root" ] || continue
  if grep -rIlP --exclude-dir=node_modules --exclude-dir=site-packages --exclude-dir=.git -- "$pattern" "$root" >/tmp/scan_hits 2>/dev/null; then
    echo "credential-shaped value found in:" >&2
    grep -rInP --exclude-dir=node_modules --exclude-dir=site-packages --exclude-dir=.git -o -- "$pattern" "$root" | cut -d: -f1,2 >&2
    status=1
  fi
done
exit $status
