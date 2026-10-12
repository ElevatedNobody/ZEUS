module agni/wfp

go 1.22

require (
	github.com/tailscale/wf v0.0.0
	golang.org/x/sys v0.0.0-20211019181941-9d821ace8654
)

require (
	github.com/BurntSushi/toml v0.4.1 // indirect
	go4.org/netipx v0.0.0-20220725152314-7e7bdc8411bf // indirect
	golang.org/x/exp/typeparams v0.0.0-20220218215828-6cf2b201936e // indirect
	golang.org/x/mod v0.6.0-dev.0.20220419223038-86c51ed26bb4 // indirect
	golang.org/x/tools v0.1.11-0.20220513221640-090b14e8501f // indirect
	honnef.co/go/tools v0.3.2 // indirect
)

// The WFP library is the copy of github.com/tailscale/wf shipped in
// third_party/wf (BSD-3-Clause); nothing is fetched for it.
replace github.com/tailscale/wf => ../../third_party/wf
