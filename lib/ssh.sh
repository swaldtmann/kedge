# lib/ssh.sh — shared ssh_box() helper for verify.sh / test.sh.
#
# Sourced, not executed. Callers set SCRIPT_DIR before sourcing:
#   source "$SCRIPT_DIR/lib/ssh.sh"

SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes"

ssh_box() {
    local ip="$1"; shift
    if [[ $# -le 1 ]]; then
        ssh $SSH_OPTS "root@$ip" "$@"
    else
        # ssh joins multiple trailing argv elements with unquoted spaces and
        # hands that single string to the remote login shell to re-parse
        # before `bash -s` ever sees $1..$N -- an unquoted '$'/backtick in
        # e.g. a restic password gets expanded/executed by that remote
        # shell first. %q-quote each piece so it survives as literal data.
        local remote_cmd
        remote_cmd="$(printf '%q ' "$@")"
        ssh $SSH_OPTS "root@$ip" "$remote_cmd"
    fi
}
