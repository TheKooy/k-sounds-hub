#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="${1:-$PWD}"
cd -- "$repo_root"

if [[ ! -d .git || ! -f src/ksound_hub/glass_app.py ]]; then
    echo "Erreur: lancer ce script avec le dépôt K-Sounds Hub en argument." >&2
    exit 2
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
stage="$(mktemp -d "${TMPDIR:-/tmp}/ksounds-remote-audit.XXXXXX")"
trap 'rm -r -- "$stage"' EXIT

mkdir -p -- "$stage/repo"

mapfile -t selected < <(
    {
        # Include tracked and relevant untracked work. SoundboardService and
        # regression tests are intentionally created before the next commit.
        git ls-files --cached --others --exclude-standard |
            grep -Ei 'soundboard|remote|android|(^|/)(pyproject\.toml|PROJECT_STATUS\.md)$'
        printf '%s\n' \
            src/ksound_hub/glass_app.py \
            src/ksound_hub/models.py \
            src/ksound_hub/settings_store.py \
            src/ksound_hub/ipc.py
    } | sort -u
)

if (( ${#selected[@]} == 0 )); then
    echo "Erreur: aucun fichier Soundboard/Remote n'a été trouvé." >&2
    exit 2
fi

for path in "${selected[@]}"; do
    [[ -f "$path" ]] || continue
    mkdir -p -- "$stage/repo/$(dirname -- "$path")"
    cp -- "$path" "$stage/repo/$path"
done

{
    git status --short --branch
    git log -12 --oneline
} > "$stage/repo/git-state.txt"

git diff -- "${selected[@]}" > "$stage/repo/targeted-current-diff.patch"

output_dir="${XDG_DOWNLOAD_DIR:-$HOME/Downloads}"
mkdir -p -- "$output_dir"
archive="$output_dir/K-Sounds-Soundboard-Remote-audit-${timestamp}.tar.gz"
tar --sort=name --owner=0 --group=0 --numeric-owner -C "$stage" -czf "$archive" repo
sha256sum "$archive" > "$archive.sha256"

echo "$archive"
echo "$archive.sha256"
