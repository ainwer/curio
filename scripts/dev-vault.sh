#!/bin/sh
# Create the local dev vault (bare remote + clone) used by the dev server.
# Run this before starting the dev server (see AGENTS.md "Commands").

set -eu

cd "$(dirname "$0")/.."

remote=".cache/dev-remote.git"
clone=".cache/dev-vault"

if [ ! -d "$remote" ]; then
  git init --bare --initial-branch=main --quiet "$remote"
fi

if [ ! -d "$clone/.git" ]; then
  git clone --quiet "$remote" "$clone" >/dev/null 2>&1 || git clone --quiet "$remote" "$clone"
  current_branch=$(git -C "$clone" symbolic-ref --short HEAD)
  mkdir -p "$clone/Learning/Questions"
  cat > "$clone/Learning/Knowledge-Profile.md" <<'EOF'
# Knowledge profile

- Knows: analog power stages (buck, flyback), soldering, multimeter work.
- Knows: small Python automation scripts at home.
- Learning: digital control loops and firmware basics.
- Uses: a home lab bench with a variac and an oscilloscope.
- Wants: to size magnetics and plan EMI filters with more confidence.
EOF
  touch "$clone/Learning/Questions/.gitkeep"
  git -C "$clone" add -A
  git -C "$clone" -c user.name=curio -c user.email=curio@localhost commit --quiet -m "curio: seed dev vault"
  git -C "$clone" push --quiet -u origin "$current_branch:main" 2>/dev/null || \
    git -C "$clone" push --quiet -u origin "$current_branch"
fi

echo "Dev vault ready at $clone (remote: $remote)"