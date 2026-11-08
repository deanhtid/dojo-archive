# Login

The tool reuses a saved session. When the session has ended, it asks for the account email and the code that ClassDojo emails.

## Sub-features

- `login-reuse` runs with no prompt while the session is live.
- `login-no-terminal` stops with a plain sentence when no terminal can answer the prompts.
- `login-code-prompt` asks for the email and the emailed code at a terminal.

## How to get to it (user POV)

- Run `uv run dojo-archive` in a terminal window.

## Driving it with capture.sh

Preconditions:

- The baseline preconditions pass for `login-reuse`.
- `login-code-prompt` needs the account owner to agree, to be at the terminal, and to read the emailed code. Skip it otherwise, and report it as not run.

- **Reuse.** The `sync-download` run proves this, because `capture.sh` closes stdin and the run still ends with `exit=0`.
- **No terminal.** Run `HOME="$PWD/.verify-runs/RUN_ID/home" .agents/skills/verify-dojo/capture.sh RUN_ID login-no-terminal uv run dojo-archive --output .verify-runs/RUN_ID/out` after `mkdir -p .verify-runs/RUN_ID/home`. The empty home folder has no session. The transcript shows `Run this in a terminal window.` and `exit=1`. No email arrives, because the prompt stops before any request.
- **Emailed code.** With the owner's agreement, use a home folder with no session as above, and let the owner run the same command in their own terminal, with no `capture.sh`. They type the email, then the code. The screen shows `Logged in. You stay logged in on this computer.` and later `Found N posts`. Ask them to confirm those lines. Do not read the code or the email yourself.

## Gotchas

- A code works only with the device cookies from the request that sent it. The tool saves them after each request, also after a failed code.
- Each new login sends a new email, and it makes older codes useless. A wrong code retries up to 3 times with no new email.
- ClassDojo locks an account after repeated wrong logins. Do not retry a refused login in a loop.
- `capture.sh` returns 1 when the command does. The `login-no-terminal` step expects that, so read `exit=1` in the transcript.
