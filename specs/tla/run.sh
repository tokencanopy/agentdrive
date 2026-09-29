#!/usr/bin/env bash
# TLA+ spec harness (specs/tla/README.md).
#
# Three jobs, in order:
#   1. Fetch tla2tools.jar, pinned by release tag + sha256, into .cache/.
#   2. Translation drift guard: re-run pcal.trans on a COPY of each spec and
#      diff against the checked-in file — same idea as the `make css` drift
#      guard. Fails if someone edited the PlusCal block without re-translating
#      (or hand-edited the generated TRANSLATION section).
#   3. Model-check every config against its EXPECTED outcome. Bug configs
#      must produce an invariant violation — they are regression artifacts:
#      if a bug config ever comes up clean, either the model drifted or the
#      spec's semantics changed silently. Fix configs must check clean.
#
# Usage: run.sh [--full]
#   default: fast configs + CI-scoped bounds (~5 min)
#   --full:  adds the full exhaustive fix configs (adds ~10-30 min)
# Env: TLA_WORKERS (default: auto), JAVA (path to java binary).
set -euo pipefail
cd "$(dirname "$0")"

TLA_VERSION=v1.7.4
TLA_SHA256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
CACHE_DIR=.cache
JAR="$CACHE_DIR/tla2tools-$TLA_VERSION.jar"
WORKERS="${TLA_WORKERS:-auto}"
FULL=0
[[ "${1:-}" == "--full" ]] && FULL=1

# --- java discovery ---------------------------------------------------------
# Candidates are validated by actually running `-version`: macOS ships a
# /usr/bin/java stub that exists but errors when no runtime is installed.
if [[ -z "${JAVA:-}" ]]; then
  for cand in java /opt/homebrew/opt/openjdk/bin/java /usr/local/opt/openjdk/bin/java; do
    if "$cand" -version >/dev/null 2>&1; then JAVA=$cand; break; fi
  done
fi
if [[ -z "${JAVA:-}" ]] || ! "$JAVA" -version >/dev/null 2>&1; then
  echo "error: no working java — install with \`brew install openjdk\` (macOS)" >&2
  exit 1
fi

# --- pinned toolchain fetch -------------------------------------------------
mkdir -p "$CACHE_DIR"
if [[ ! -f "$JAR" ]]; then
  echo "fetching tla2tools.jar $TLA_VERSION ..."
  curl -fsSL -o "$JAR.tmp" \
    "https://github.com/tlaplus/tlaplus/releases/download/$TLA_VERSION/tla2tools.jar"
  mv "$JAR.tmp" "$JAR"
fi
ACTUAL=$(python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$JAR")
if [[ "$ACTUAL" != "$TLA_SHA256" ]]; then
  echo "error: tla2tools.jar sha256 mismatch (got $ACTUAL) — delete $JAR and retry" >&2
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FAILED=0

# --- 1. translation drift guard --------------------------------------------
for spec in UploadGC ArtifactGC ReservationAccounting FolderNamespace; do
  cp "$spec.tla" "$TMP/"
  "$JAVA" -cp "$JAR" pcal.trans -nocfg "$TMP/$spec.tla" >/dev/null
  if ! diff -q "$spec.tla" "$TMP/$spec.tla" >/dev/null; then
    echo "FAIL  $spec.tla: TRANSLATION section is stale — re-run:"
    echo "      java -cp $JAR pcal.trans $spec.tla"
    FAILED=1
  else
    echo "ok    $spec.tla translation in sync"
  fi
done

# --- 2. model checks against expected outcomes ------------------------------
check() {  # <config-stem> <expected: violation|clean>
  local cfg=$1 expect=$2 module out start elapsed
  module=${cfg%%_*}
  start=$SECONDS
  # TLC exits nonzero on violations — capture output either way. -metadir
  # keeps TLC's states/ scratch out of the tree.
  out=$("$JAVA" -XX:+UseParallelGC -cp "$JAR" tlc2.TLC \
        -deadlock -workers "$WORKERS" -metadir "$TMP/states-$cfg" \
        -config "$cfg.cfg" "$module.tla" 2>&1) || true
  elapsed=$((SECONDS - start))
  case $expect in
    violation)
      if grep -q "is violated" <<<"$out"; then
        echo "ok    $cfg: violation reproduced (${elapsed}s)"
      else
        echo "FAIL  $cfg: expected an invariant violation but none was found —"
        echo "      the model or spec semantics drifted (${elapsed}s)"
        FAILED=1
      fi ;;
    clean)
      if grep -q "No error has been found" <<<"$out"; then
        echo "ok    $cfg: checked clean (${elapsed}s)"
      else
        echo "FAIL  $cfg: expected clean but TLC reported:"
        grep -E "Error|is violated" <<<"$out" | head -5 | sed 's/^/      /'
        FAILED=1
      fi ;;
  esac
}

check UploadGC_bug        violation
check UploadGC_fix        clean
check ArtifactGC_bug      violation
check ArtifactGC_nodel    violation
check ArtifactGC_fix_ci   clean
check ReservationAccounting clean
check FolderNamespace_bug   violation
check FolderNamespace_probe violation
check FolderNamespace_fix   clean
if [[ $FULL == 1 ]]; then
  check ArtifactGC_fix    clean
fi

if [[ $FAILED == 1 ]]; then
  echo "TLA+ harness: FAILURES (see above)"
  exit 1
fi
echo "TLA+ harness: all checks passed"
