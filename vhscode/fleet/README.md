# Fleet workbench

### Installed Mac app

Open **Fleet Workbench** in Applications. Choose **File → Open Folder** (⌘O) or **File → Open Workspace from File** (⇧⌘O) to import a VS Code `.code-workspace` project. You can also drop folders or workspace files onto the window. Imported roots appear in the sidebar and persist between launches.

The file explorer, editor tabs, and agent controls share a native Mac window. Use ⌘P to find files, ⌘S to save, ⌘B to toggle the explorer, and ⌘W to close the current editor tab. Attach files using the paperclip beside a path in the explorer.

Use **New Agent** to add Codex or Cursor sessions to a workspace. Each session keeps its own prompt, file attachments, and native CLI context. Up to four desktop tasks can run at once on each host. Click an agent beneath its workspace in the sidebar to view its progress or stop that run. Opening a finished run shows its Markdown response, including selectable code blocks.

Type `/resume` in the composer to choose an existing session for this workspace, or use **Agent options → Resume Session…**. Fleet can resume a conversation started by the standalone CLI. Use `/new` for a fresh conversation in the current agent, `/status` to inspect it, and `/help` to see composer commands. The workspace header’s **New Agent** button adds an independent agent.

The **Model** dropdown beside the provider and view controls lists models from the selected CLI on the workspace's machine. Choosing a model applies it to that workspace and harness, including scheduled tasks. **CLI default** follows the CLI's configuration. If the account rejects the configured default, choose one of the models returned by that machine. Each scheduled task starts a fresh conversation.

Open **Activity**, **Schedules**, or **Risks** to monitor tasks across hosts. The sidebar shows running agents and paused schedules. For an SSH workspace, enter an OpenSSH alias, use **Test SSH** for interactive authentication, and browse to a project folder on the remote machine. The Intel Mac bundle includes Python, Git, tmux, both agent CLIs, and the local MobileBERT model. The remote machine supplies Python 3.10+, its agent CLIs and their login sessions.

If Cursor needs authentication, select its agent and click **Sign In to Cursor** in the composer or agent options. Approve the browser page on this Mac. Cursor saves the login on the selected workspace machine, and Fleet verifies its model catalog. If Cursor requests workspace trust, **Trust Workspace for Cursor…** opens a confirmation for that folder. You can revoke Fleet’s trust setting in agent options.

Choose **Agent options → Edit Agent Command…** or type `/command` in the composer to change the command for the selected provider and workspace. Enter a name such as `agent`, or an executable path such as `~/.local/bin/agent` to pick a specific install. Quote paths that contain spaces. Optional arguments are passed before the CLI's operation arguments. **Test Command** checks which executable responds to `--version` on the workspace machine. Clear the command to restore automatic discovery. The same setting applies to background runs, schedules, model discovery, and native sessions.

Choose **Open Agent in Terminal** or type `/native` to use the CLI interactively in the workspace and resume the current session when available. Use this to respond to Codex or Cursor trust and approval prompts. **Open Workspace Shell** or `/shell` opens a login shell for checking the installation, signing in manually, or running a CLI command directly. For SSH workspaces, Terminal connects to the workspace machine using your OpenSSH settings. You can also launch a shell from the SSH workspace settings while troubleshooting a connection.

### Running from a source checkout

Use the fork's launcher from this checkout. Fleet runs on macOS or Linux with Python 3.10+ and a terminal. Install and authenticate the Codex or Cursor CLI on each machine where you want that agent to work.

```sh
./vhscode/bin/vhscode fleet ~/src/project
./vhscode/bin/vhscode fleet --ssh build-01 /srv/project
./vhscode/bin/vhscode fleet --ssh build-01 --agent cursor --persist /srv/project
```

For the lowest local startup cost, run `./vhscode/fleet/fleet.py` directly, or use `vhscode-fleet` when this branch’s package is installed. Both start directly in Python.

An SSH destination can be a host alias from your OpenSSH config, including its user, port, identity and jump host. The repo argument is a directory on that machine. Quote a remote home-relative path such as `'~/src/project'` so the local shell leaves it intact. Fleet uses the installed macOS app’s Python when available, or `python3` (3.10+) on the remote shell’s PATH. Fleet looks for agents on PATH, in common user bin directories, and in the installed macOS app. Remote machines do not need Node, a checkout of this fork, or a running service. If Cursor reports a locked login keychain over SSH, use Fleet or Terminal in the desktop session on the Mac running Cursor.

## Fleet dashboard

Launch `./vhscode/fleet/fleet.py` with no arguments for the dashboard. You can also open it with `vhscode fleet --dashboard` or `/dashboard` at the workbench prompt. The inventory belongs to the machine running the dashboard. Open it on your laptop for a fleet-wide view. Successfully connecting with `--ssh` adds that destination to its inventory.

The Activity view lists running tasks and recent completions across machines. Press Enter on a task to inspect its history. Tab cycles through Hosts, Schedules, and Risks. Inspect a run for the public plan, actions, and outcome reported by its harness. Open the schedules view to choose the target repository, Codex or Cursor, prompt, repeat interval in minutes, and reporting mode. You can edit, pause, resume, and remove schedules there. Opening a host interactively lets you complete a first SSH login or host-key prompt. Background polling uses key-based login or an existing authenticated connection.

Each host stores its schedules and bounded run history under `${XDG_STATE_HOME:-$HOME/.local/state}/vhscode/fleet`. A detached user process runs enabled schedules even with the dashboard closed. The host runs up to two scheduled tasks concurrently, skips missed intervals after downtime, and limits each run to 30 minutes. Pausing or deleting a schedule also cancels its active run. After a machine reboots, reconnect with the dashboard to wake that scheduler. The home directory and agent credentials must be available to the SSH user.

### Reporting settings

| Mode | Terminal output |
| --- | --- |
| Actions | Main tool actions, errors and outcome. |
| Summary | A short outcome and any blockers. |
| Risks | Reported test failures, approvals or potentially breaking changes, plus the outcome. |

Choose a mode in each schedule’s form. In an interactive repository, `/view summary` saves its default. Use `/risks review the migration` for one turn. The Risks tab collects flagged activity across hosts. Open a run for the public plans, actions and evidence behind the flag. Risk indicators depend on what the harness reports.

## Working in a repository

- Enter `/files` to browse the directory tree, including dotfiles. Enter opens the selected directory or file. Space toggles a file tag. The editor uses the target machine's `$VISUAL`, `$EDITOR`, or `vi`. Save and exit to return to the browser. Press `q` to return to the prompt.
- Enter `/tag src/main.py` to attach a known path directly. Spaces in a path are literal. Tags persist across prompts until removed with `/untag PATH` or `/untag-all`. Use `/tags` to see the selection. Harnesses receive repository-relative paths and read the files they need.
- Type a prompt to start a turn. The default view emphasizes actions and the outcome. Use `/details` for more harness output. Use `/agent cursor` or `/agent codex` to switch. Each agent resumes its own conversation within the workbench. `/new` starts a fresh conversation for the selected agent. `/native` opens its interactive CLI for approvals or native commands. If a Fleet turn already has a session ID, the native CLI resumes it.
- Enter `/command` to show the selected provider's command, `/command agent` to change it, or `/command --default` to restore automatic discovery. Use `/shell` to open a login shell, or start the launcher with `--native` or `--shell` to open either directly. These options work with `--ssh HOST`.
- Use `/status` and `/diff` to review the working tree. Ctrl-C interrupts an active turn. `/quit` closes the workbench.
- Use `--persist` on a host with tmux to keep the running workbench, its tags, and its active turn through a disconnect. Detach with Ctrl-B then D. Run the same command for the same remote directory to reattach. A plain SSH session ends when its terminal closes.

## Output filtering

On the machine running the workbench, enter `/filter-setup` once to fetch the pinned quantized MobileBERT model and install its CPU runtime into a user-cache virtual environment. Use `/details` to show public progress, then enter `/filter` to classify eligible progress. You can also enable the filter flag at startup with `--filter`. Model setup requires network access and a compatible Python interpreter. Inference runs locally. You can verify installation directly from the checkout with `python3 vhscode/fleet/fleet_filter.py smoke`.

Only eligible transient events enter the classifier. Final assistant messages, warnings, errors and approval events go straight to the terminal. Under load, on timeout, or if the optional runtime is unavailable, the workbench shows the progress event. The CLI's own session history remains available in the native harness.

## Architecture and performance

| Component | Work performed |
| --- | --- |
| Launcher | Dispatches directly into the Python terminal interface before initializing the OpenCode sidecars. |
| SSH transport | Sends a small compressed Python bundle through OpenSSH, installs it atomically under `~/.cache/vhscode/fleet/<digest>`, and attaches a PTY on the same authenticated connection. Only terminal I/O crosses that connection during editing. |
| Dashboard and scheduler | Poll hosts in the background and persist host-local schedules in SQLite. The dashboard fetches individual histories on demand. Each host retains up to 100 finished runs and 80 events per run. |
| File browser | Enumerates one directory on demand and renders a page of entries. The native editor owns the open buffer. The workbench keeps paths for tagged files. |
| Agent adapters | Spawn the installed harness in the target directory, consume its event stream incrementally, and retain a session ID per harness. Each CLI owns its credentials, permissions, tool execution and conversation history. |
| Progress worker | Starts on the first eligible event with filtering enabled. The queue and input lengths are bounded. Inference runs in a separate single-CPU-thread process and the supervisor releases it after an idle timeout. |

SSH runtime directories are shared by sessions using identical versions. A running workbench or scheduler can still use a cached runtime. Remove old versions only after closing those sessions and pausing schedules on the host. The optional classifier lives separately under `${XDG_CACHE_HOME:-$HOME/.cache}/opencode/fleet-filter`.
