# Default keys

The prefix inherits the user's tmux configuration unless `keys.prefix` is set.
Bindings can be changed or disabled (`null`) in `keys.bindings`.

| After prefix | Action | Configuration name |
| --- | --- | --- |
| `n` / `p` | Next / previous session | `next` / `previous` |
| `l` | Sidebar | `sidebar` |
| `c` | Chat pane | `chat` |
| `e` | Plan pane | `plan` |
| `g` | Focus current plan task; while leased, focus without steering the editor | `jump_plan` |
| `m` | Focus this session's leased pane; message if none | `lease` |
| `s` | Scratchpad popup | `scratchpad` |
| `a` | Agent menu | `agent` |
| `i` | Session information | `card` |
| `o` | Session options | `options` |
| `r` | Reset layout | `reset` |
| `k` | Keys help | `help` |
| `+` | New session | `new` |
| `x` | Error details | `error` |

See [pane leases](pane-leases.md) for the command and restoration contract.
