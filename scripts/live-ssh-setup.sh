#!/usr/bin/env bash
# A real SSH host on this machine, for the suite's live SSH and sudo tests
# (#234, batch 226). tests.yml's `live-ssh` job runs it on a throwaway
# runner. Run it anywhere else only on a machine you would throw away too:
# it creates an account with a password and sudo rights, and starts two
# sshd daemons on 127.0.0.1.
#
# What it stands up is every shape the live tests ask about:
#   - port 2222: a key, a passphrase-protected key, or the account
#     password, any one of them;
#   - port 2223: `publickey,password`, the stacked case, which needs both;
#   - an account whose sudo asks for its own password.
# Each secret is random per run. The VEN_TEST_* variables the tests read
# go to the file named by $1 as NAME=value lines; CI passes $GITHUB_ENV.
# `test_the_live_ssh_job_sets_every_variable_the_tests_read` holds this
# list to the names the tests use.
set -euo pipefail

OUT=${1:?usage: live-ssh-setup.sh <env-file>}
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"
ACCOUNT=ventest
PORT=2222
STACKED_PORT=2223
SERVER=/etc/ssh/venastine-live
CLIENT=$(mktemp -d)
chmod 700 "$CLIENT"

if [ ! -x /usr/sbin/sshd ] || ! command -v sudo >/dev/null \
        || ! command -v ssh >/dev/null; then
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        openssh-server openssh-client sudo
fi

secret() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
PASSWORD=$(secret)
KEYPASS=$(secret)
if [ -n "${GITHUB_ACTIONS:-}" ]; then
    echo "::add-mask::$PASSWORD"
    echo "::add-mask::$KEYPASS"
fi

# The account, its password, its sudo rule and its workspace.
id "$ACCOUNT" >/dev/null 2>&1 || $SUDO useradd -m -s /bin/bash "$ACCOUNT"
echo "$ACCOUNT:$PASSWORD" | $SUDO chpasswd
echo "$ACCOUNT ALL=(ALL:ALL) ALL" | $SUDO tee /etc/sudoers.d/venastine-live >/dev/null
$SUDO chmod 0440 /etc/sudoers.d/venastine-live
HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)
$SUDO install -d -m 700 -o "$ACCOUNT" -g "$ACCOUNT" "$HOME_DIR/.ssh" "$HOME_DIR/ws"

# The client's two keys, and the account accepting both.
ssh-keygen -q -t ed25519 -N '' -f "$CLIENT/key"
ssh-keygen -q -t ed25519 -N "$KEYPASS" -f "$CLIENT/enckey"
cat "$CLIENT/key.pub" "$CLIENT/enckey.pub" \
    | $SUDO tee "$HOME_DIR/.ssh/authorized_keys" >/dev/null
$SUDO chown "$ACCOUNT:$ACCOUNT" "$HOME_DIR/.ssh/authorized_keys"
$SUDO chmod 600 "$HOME_DIR/.ssh/authorized_keys"

# Two servers, each with its own host key and config.
$SUDO install -d -m 755 "$SERVER" /run/sshd
for port in "$PORT" "$STACKED_PORT"; do
    $SUDO rm -f "$SERVER/host_$port" "$SERVER/host_$port.pub"
    $SUDO ssh-keygen -q -t ed25519 -N '' -f "$SERVER/host_$port"
    {
        echo "Port $port"
        echo "ListenAddress 127.0.0.1"
        echo "HostKey $SERVER/host_$port"
        echo "PidFile /run/sshd-venastine-$port.pid"
        echo "AllowUsers $ACCOUNT"
        echo "PubkeyAuthentication yes"
        echo "PasswordAuthentication yes"
        echo "KbdInteractiveAuthentication no"
        echo "UsePAM yes"
        if [ "$port" = "$STACKED_PORT" ]; then
            echo "AuthenticationMethods publickey,password"
        fi
    } | $SUDO tee "$SERVER/sshd_$port.conf" >/dev/null
    $SUDO /usr/sbin/sshd -t -f "$SERVER/sshd_$port.conf"
    $SUDO /usr/sbin/sshd -f "$SERVER/sshd_$port.conf" -E "$SERVER/sshd_$port.log"
done

# Each server's host key as ssh-keyscan prints it: what a user pastes into
# `ssh_host_key`, and what the tests pin.
for port in "$PORT" "$STACKED_PORT"; do
    for _ in $(seq 1 50); do
        if ssh-keyscan -p "$port" 127.0.0.1 > "$CLIENT/known_$port" 2>/dev/null \
                && [ -s "$CLIENT/known_$port" ]; then
            break
        fi
        sleep 0.2
    done
    [ -s "$CLIENT/known_$port" ] || { echo "sshd on $port never answered" >&2; exit 1; }
done

{
    echo "VEN_TEST_SSH_HOST=127.0.0.1"
    echo "VEN_TEST_SSH_PORT=$PORT"
    echo "VEN_TEST_SSH_USER=$ACCOUNT"
    echo "VEN_TEST_SSH_KEY=$CLIENT/key"
    echo "VEN_TEST_SSH_HOSTKEY=$CLIENT/known_$PORT"
    echo "VEN_TEST_SSH_WS=$HOME_DIR/ws"
    echo "VEN_TEST_SSH_ENCKEY=$CLIENT/enckey"
    echo "VEN_TEST_SSH_KEYPASS=$KEYPASS"
    echo "VEN_TEST_SSH_PASSWORD=$PASSWORD"
    echo "VEN_TEST_SSH_STACKED_PORT=$STACKED_PORT"
    echo "VEN_TEST_SSH_STACKED_HOSTKEY=$CLIENT/known_$STACKED_PORT"
    echo "VEN_TEST_SUDO_PW=$PASSWORD"
} >> "$OUT"
echo "live SSH host ready: $ACCOUNT@127.0.0.1, ports $PORT and $STACKED_PORT"
