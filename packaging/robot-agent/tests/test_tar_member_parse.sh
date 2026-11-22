#!/bin/sh
# The agent tarball extraction guard.
#
# `download_and_verify_agent` reads `tar -tvzf` and reconstructs each member
# name with `tr -s ' ' | cut -d' ' -f6-`. That parse is the only thing standing
# between a hostile archive and a root-owned extraction, and it is exactly the
# kind of field-counting that works on the archives you built and fails on the
# one an attacker builds. So: build real archives here -- with spaces in names,
# with symlinks, with setuid bits, with an escaping path -- and run the shipped
# loop over them.
#
# GNU tar and busybox tar format `-tv` slightly differently (busybox prints
# seconds in the timestamp), so this suite is meaningful under both, which is
# why it is a test_*.sh and runs in the container gate too.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='tar member parsing'

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

ROOT_NAME='ingotron-agent-0.9.9'

# The shipped loop, extracted verbatim, wrapped so its `die` is observable.
run_member_scan() {
    ( TMPDIR_SELF="$WORK/scan"
      rm -rf "$TMPDIR_SELF"; mkdir -p "$TMPDIR_SELF"
      AGENT_ROOT_DIR=$ROOT_NAME
      AGENT_SIZE=$(wc -c < "$1" | tr -d ' ')
      AGENT_SHA256=$(openssl dgst -sha256 -r "$1" | cut -d' ' -f1)
      VAULT='https://vault.test'
      EX_ARTIFACT=4
      die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
      # curl is replaced by a copy of the fixture, so the whole function runs.
      curl() {
          _dest=''
          while [ $# -gt 0 ]; do
              case $1 in -o) _dest=$2; shift 2 ;; *) shift ;; esac
          done
          cp "$FIXTURE" "$_dest"
      }
      FIXTURE=$1
      eval "$(extract_function file_size)"
      eval "$(extract_function sha256_file)"
      eval "$(extract_function fetch_public)"
      eval "$(extract_function download_and_verify_agent)"
      download_and_verify_agent
      printf 'ACCEPTED %s\n' "$STAGED_ROOT" )
}

build_tarball() {
    _bt_out=$1
    ( cd "$WORK/build" && tar -czf "$_bt_out" "$ROOT_NAME" )
}

reset_build() {
    rm -rf "$WORK/build"
    mkdir -p "$WORK/build/$ROOT_NAME/bin" "$WORK/build/$ROOT_NAME/systemd"
    printf '#!/bin/sh\n' > "$WORK/build/$ROOT_NAME/bin/ingotron-agent"
    printf '#!/bin/sh\n' > "$WORK/build/$ROOT_NAME/bin/ingotron-agent-run"
    printf '0.9.9\n' > "$WORK/build/$ROOT_NAME/VERSION"
    chmod 0755 "$WORK/build/$ROOT_NAME/bin/"*
}

# --- the benign archive must be ACCEPTED -----------------------------------

reset_build
build_tarball "$WORK/ok.tar.gz"
assert_status 'a well-formed archive is accepted' 0 run_member_scan "$WORK/ok.tar.gz"
assert_output_contains 'ACCEPTED'

# A member name containing SPACES is the case the field-counting parse is most
# likely to get wrong: `cut -f6-` must keep the whole tail, not the first word.
reset_build
printf 'x\n' > "$WORK/build/$ROOT_NAME/a file with spaces.txt"
build_tarball "$WORK/spaces.tar.gz"
assert_status 'a member name containing spaces is still accepted' 0 \
    run_member_scan "$WORK/spaces.tar.gz"
assert_output_contains 'ACCEPTED'

# ...and a spaced name that ESCAPES the root must still be refused, which is
# the half that a truncating parse would let through.
reset_build
mkdir -p "$WORK/build/elsewhere"
printf 'x\n' > "$WORK/build/elsewhere/evil file.txt"
( cd "$WORK/build" && tar -czf "$WORK/spaced-escape.tar.gz" "$ROOT_NAME" "elsewhere/evil file.txt" )
assert_status 'a spaced member outside the root is refused' 4 \
    run_member_scan "$WORK/spaced-escape.tar.gz"
assert_output_contains 'outside'

# --- hostile members -------------------------------------------------------

reset_build
ln -s /etc/shadow "$WORK/build/$ROOT_NAME/shadow-link"
build_tarball "$WORK/symlink.tar.gz"
assert_status 'a symlink member is refused' 4 run_member_scan "$WORK/symlink.tar.gz"
assert_output_contains 'symlink'

# HARDLINKS. The guard for these was inert under busybox, which is the shell
# the bootstrap must support: GNU tar types a hardlink `h`, busybox types it `-`
# (indistinguishable from a regular file) and renders " -> target", the same
# syntax it uses for a symlink. A type-character check alone therefore passed
# hardlinks on exactly the platform the release gate requires -- and because
# the member name was extracted by stripping " -> ...", an absolute target
# never reached the absolute-path check either. Both tars are exercised.
reset_build
ln "$WORK/build/$ROOT_NAME/VERSION" "$WORK/build/$ROOT_NAME/hardlinked"
build_tarball "$WORK/hardlink.tar.gz"
assert_status 'a hardlink member is refused' 4 run_member_scan "$WORK/hardlink.tar.gz"
assert_output_contains 'hardlink'

case_start 'the hardlink refusal also holds when busybox tar does the listing'
if command -v busybox >/dev/null 2>&1; then
    _bb=$( ( PATH="$WORK/bbbin:$PATH"
             mkdir -p "$WORK/bbbin"
             printf '#!/bin/sh\nexec busybox tar "$@"\n' > "$WORK/bbbin/tar"
             chmod 0755 "$WORK/bbbin/tar"
             run_member_scan "$WORK/hardlink.tar.gz" ) 2>&1 )
    _bb_rc=$?
    case $_bb in
        *'link member'*|*hardlink*) [ "$_bb_rc" -eq 4 ] && ok || fail "refused but exit $_bb_rc: $_bb" ;;
        *) fail "busybox tar listing let a hardlink through (exit $_bb_rc):
$_bb" ;;
    esac
else
    skip 'busybox tar hardlink refusal' 'busybox is not installed'
fi

reset_build
printf 'x\n' > "$WORK/build/$ROOT_NAME/setuid-binary"
chmod 4755 "$WORK/build/$ROOT_NAME/setuid-binary"
build_tarball "$WORK/setuid.tar.gz"
assert_status 'a setuid member is refused' 4 run_member_scan "$WORK/setuid.tar.gz"
assert_output_contains 'setuid'

reset_build
printf 'x\n' > "$WORK/build/$ROOT_NAME/sticky"
chmod 1755 "$WORK/build/$ROOT_NAME/sticky"
build_tarball "$WORK/sticky.tar.gz"
assert_status 'a sticky member is refused' 4 run_member_scan "$WORK/sticky.tar.gz"

reset_build
mkdir -p "$WORK/build/other-root"
printf 'x\n' > "$WORK/build/other-root/f.txt"
( cd "$WORK/build" && tar -czf "$WORK/tworoots.tar.gz" "$ROOT_NAME" other-root )
assert_status 'a second top-level directory is refused' 4 run_member_scan "$WORK/tworoots.tar.gz"
assert_output_contains 'outside'

# A '..' component, built by naming it explicitly so tar records it verbatim.
reset_build
( cd "$WORK/build/$ROOT_NAME" && tar -czf "$WORK/dotdot.tar.gz" \
      --transform "s|^VERSION|$ROOT_NAME/../escaped|" VERSION 2>/dev/null ) \
  || ( cd "$WORK/build" && tar -czf "$WORK/dotdot.tar.gz" \
       "$ROOT_NAME" "$ROOT_NAME/../$ROOT_NAME/VERSION" 2>/dev/null ) || true
# Only assert on the fixture if the fixture is actually hostile. Both GNU tar
# (which normalises `..` away with a warning) and busybox tar (which has no
# --transform) can quietly produce a BENIGN archive here, and asserting
# "refused" against a benign archive would be a test that passes for the wrong
# reason -- or, worse, fails and gets "fixed" by weakening the guard.
if [ -s "$WORK/dotdot.tar.gz" ] && tar -tzf "$WORK/dotdot.tar.gz" 2>/dev/null | grep -q '\.\.'; then
    assert_status "a member with a '..' component is refused" 4 \
        run_member_scan "$WORK/dotdot.tar.gz"
else
    skip "a member with a '..' component is refused" \
         "this tar ($(tar --version 2>&1 | head -n 1)) normalises '..' away rather than recording it, so no hostile fixture could be built; the guard is asserted statically below"
fi

# THE case that discriminates a whole-name parse from a truncating one.
#
# A member named `<root>/x ../../etc/evil` has a FIRST WORD that is perfectly
# well-behaved: it lives under the declared root and contains no `..`. Only the
# tail escapes. A parse that reads `cut -f6` instead of `cut -f6-` validates
# the harmless prefix and extracts the hostile whole -- and every other fixture
# in this file passes under that mutation, which is how the gap was found
# (check_mutation_guards.sh).
reset_build
if ( cd "$WORK/build/$ROOT_NAME" \
     && tar -czf "$WORK/split-escape.tar.gz" \
        --transform "s|^VERSION|$ROOT_NAME/x ../../etc/evil|" VERSION ) 2>/dev/null \
   && tar -tzf "$WORK/split-escape.tar.gz" 2>/dev/null | grep -q '\.\.'; then
    assert_status 'a member whose FIRST WORD is benign but whose tail escapes is refused' 4 \
        run_member_scan "$WORK/split-escape.tar.gz"
    assert_output_contains 'parent-directory reference'
else
    skip 'a member whose FIRST WORD is benign but whose tail escapes is refused' \
         "this tar ($(tar --version 2>&1 | head -n 1)) cannot record such a member"
fi

case_start "the shipped loop rejects '..' by pattern"
if extract_function download_and_verify_agent | grep -q '\*\.\.\*)'; then ok; else
    fail "the '..' guard is not present in download_and_verify_agent"
fi

case_start 'the shipped loop rejects absolute paths by pattern'
if extract_function download_and_verify_agent | grep -q '^ *\/\*)'; then ok; else
    fail 'the absolute-path guard is not present in download_and_verify_agent'
fi

# --- size and hash, in that order ------------------------------------------

reset_build
build_tarball "$WORK/hash.tar.gz"
run_wrong_hash() {
    ( TMPDIR_SELF="$WORK/scan2"
      rm -rf "$TMPDIR_SELF"; mkdir -p "$TMPDIR_SELF"
      AGENT_ROOT_DIR=$ROOT_NAME
      AGENT_SIZE=$(wc -c < "$WORK/hash.tar.gz" | tr -d ' ')
      AGENT_SHA256='0000000000000000000000000000000000000000000000000000000000000000'
      VAULT='https://vault.test'
      EX_ARTIFACT=4
      die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
      curl() { _dest=''; while [ $# -gt 0 ]; do case $1 in -o) _dest=$2; shift 2 ;; *) shift ;; esac; done; cp "$WORK/hash.tar.gz" "$_dest"; }
      eval "$(extract_function file_size)"
      eval "$(extract_function sha256_file)"
      eval "$(extract_function fetch_public)"
      eval "$(extract_function download_and_verify_agent)"
      download_and_verify_agent )
}
assert_status 'a sha256 mismatch is refused' 4 run_wrong_hash
assert_output_contains 'sha256 mismatch'

run_wrong_size() {
    ( TMPDIR_SELF="$WORK/scan3"
      rm -rf "$TMPDIR_SELF"; mkdir -p "$TMPDIR_SELF"
      AGENT_ROOT_DIR=$ROOT_NAME
      AGENT_SIZE=999999999
      AGENT_SHA256=$(openssl dgst -sha256 -r "$WORK/hash.tar.gz" | cut -d' ' -f1)
      VAULT='https://vault.test'
      EX_ARTIFACT=4
      die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
      curl() { _dest=''; while [ $# -gt 0 ]; do case $1 in -o) _dest=$2; shift 2 ;; *) shift ;; esac; done; cp "$WORK/hash.tar.gz" "$_dest"; }
      eval "$(extract_function file_size)"
      eval "$(extract_function sha256_file)"
      eval "$(extract_function fetch_public)"
      eval "$(extract_function download_and_verify_agent)"
      download_and_verify_agent )
}
assert_status 'a size mismatch is refused' 4 run_wrong_size
assert_output_contains 'size mismatch'

summary
