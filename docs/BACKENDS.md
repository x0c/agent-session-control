# Control backends

Status: implementation pending. The engine worker records the extracted runtime/terminal mechanisms, host injection hooks, SessKit observation, process/input ownership, persistent supervision, cancellation evidence and tested per-runtime limits here.

Do not claim runtime or platform coverage before actual returned values or owned terminal behavior establishes it. Tests must not attach to, stop or mutate existing user sessions. Native history interpretation remains owned by SessKit.
