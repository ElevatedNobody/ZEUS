// agni-wfp: AGNI's Windows Filtering Platform helper, built on
// github.com/tailscale/wf (vendored in third_party/wf, BSD-3-Clause).
//
// It installs, reports and removes ONE persistent set of WFP filters owned by
// the "AGNI Sovereign Firewall" provider:
//
//	for each program AGNI runs as (the Python interpreter, Qt WebEngine and,
//	optionally, Ollama):
//	  permit  -> loopback (127.0.0.0/8, ::1)
//	  permit  -> private LAN ranges        (only with -allow-lan)
//	  block   -> everything else, outbound and inbound
//
// WFP is the engine underneath Windows Firewall, so these filters apply even
// if Windows Firewall rules are changed. Changing WFP needs an elevated
// (administrator) process; reading it may too. Every command prints one JSON
// object on stdout so AGNI can show exactly what happened.
//
//	agni-wfp status
//	agni-wfp apply  -app C:\path\python.exe [-app ...] [-allow-lan]
//	agni-wfp remove
//	agni-wfp drops  [-minutes 60]
//
// A leading --out=FILE also writes the JSON result to FILE. AGNI uses it when
// it starts the helper elevated (UAC), because an elevated child's stdout
// cannot be captured by the non-elevated parent.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/netip"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/tailscale/wf"
	"golang.org/x/sys/windows"
)

const version = "1.0.0"

var outPath string

var (
	providerID = wf.ProviderID(mustGUID("{4a47e5b2-6d1f-4c1e-9a3b-8f2d0c7e1a01}"))
	sublayerID = wf.SublayerID(mustGUID("{4a47e5b2-6d1f-4c1e-9a3b-8f2d0c7e1a02}"))

	loopback = []netip.Prefix{netip.MustParsePrefix("127.0.0.0/8"), netip.MustParsePrefix("::1/128")}
	lan      = []netip.Prefix{
		netip.MustParsePrefix("10.0.0.0/8"), netip.MustParsePrefix("172.16.0.0/12"),
		netip.MustParsePrefix("192.168.0.0/16"), netip.MustParsePrefix("169.254.0.0/16"),
		netip.MustParsePrefix("fc00::/7"), netip.MustParsePrefix("fe80::/10"),
	}
	layers = []struct {
		id   wf.LayerID
		name string
		v6   bool
	}{
		{wf.LayerALEAuthConnectV4, "outbound IPv4", false},
		{wf.LayerALEAuthConnectV6, "outbound IPv6", true},
		{wf.LayerALEAuthRecvAcceptV4, "inbound IPv4", false},
		{wf.LayerALEAuthRecvAcceptV6, "inbound IPv6", true},
	}
)

const (
	weightLoopback = 1000
	weightLAN      = 900
	weightBlock    = 100
)

type multi []string

func (m *multi) String() string     { return strings.Join(*m, ";") }
func (m *multi) Set(v string) error { *m = append(*m, v); return nil }

func mustGUID(s string) windows.GUID {
	g, err := windows.GUIDFromString(s)
	if err != nil {
		panic(err)
	}
	return g
}

func emit(v map[string]any) {
	v["tool"] = "agni-wfp " + version
	out, _ := json.MarshalIndent(v, "", "  ")
	fmt.Println(string(out))
	if outPath != "" {
		_ = os.WriteFile(outPath, out, 0o600)
	}
}

func fail(err error, code int) {
	msg := err.Error()
	if errors.Is(err, windows.ERROR_ACCESS_DENIED) || strings.Contains(strings.ToLower(msg), "access is denied") {
		msg = "access denied - run this from an administrator (elevated) prompt"
	}
	emit(map[string]any{"ok": false, "error": msg})
	os.Exit(code)
}

func session() *wf.Session {
	s, err := wf.New(&wf.Options{Name: "AGNI WFP helper", Description: "AGNI sovereign firewall",
		TransactionStartTimeout: 15 * time.Second})
	if err != nil {
		fail(fmt.Errorf("could not open the Windows Filtering Platform: %w", err), 3)
	}
	return s
}

func ours(s *wf.Session) ([]*wf.Rule, bool, bool) {
	rules, err := s.Rules()
	if err != nil {
		fail(fmt.Errorf("could not list WFP filters: %w", err), 3)
	}
	var mine []*wf.Rule
	for _, r := range rules {
		if r.Provider == providerID {
			mine = append(mine, r)
		}
	}
	providers, _ := s.Providers()
	haveProvider := false
	for _, p := range providers {
		if p.ID == providerID {
			haveProvider = true
		}
	}
	sublayers, _ := s.Sublayers(providerID)
	haveSublayer := false
	for _, sl := range sublayers {
		if sl.ID == sublayerID {
			haveSublayer = true
		}
	}
	return mine, haveProvider, haveSublayer
}

func describe(r *wf.Rule) map[string]any {
	action := "block"
	if r.Action == wf.ActionPermit {
		action = "permit"
	}
	return map[string]any{"name": r.Name, "action": action, "layer": r.Layer.String(),
		"weight": r.Weight, "kernel_id": r.KernelID, "description": r.Description}
}

func cmdStatus() {
	s := session()
	defer s.Close()
	mine, provider, sublayer := ours(s)
	var list []map[string]any
	for _, r := range mine {
		list = append(list, describe(r))
	}
	sort.Slice(list, func(i, j int) bool { return fmt.Sprint(list[i]["name"]) < fmt.Sprint(list[j]["name"]) })
	emit(map[string]any{"ok": true, "installed": len(mine) > 0, "provider": provider,
		"sublayer": sublayer, "filters": len(mine), "rules": list})
}

func removeAll(s *wf.Session) int {
	mine, provider, sublayer := ours(s)
	for _, r := range mine {
		if err := s.DeleteRule(r.ID); err != nil {
			fail(fmt.Errorf("could not delete filter %q: %w", r.Name, err), 4)
		}
	}
	if sublayer {
		if err := s.DeleteSublayer(sublayerID); err != nil {
			fail(fmt.Errorf("could not delete the AGNI sublayer: %w", err), 4)
		}
	}
	if provider {
		if err := s.DeleteProvider(providerID); err != nil {
			fail(fmt.Errorf("could not delete the AGNI provider: %w", err), 4)
		}
	}
	return len(mine)
}

func cmdRemove() {
	s := session()
	defer s.Close()
	n := removeAll(s)
	emit(map[string]any{"ok": true, "removed_filters": n})
}

func cmdApply(args []string) {
	fs := flag.NewFlagSet("apply", flag.ExitOnError)
	var apps multi
	fs.Var(&apps, "app", "program to confine (repeatable)")
	allowLAN := fs.Bool("allow-lan", false, "also permit private LAN ranges")
	_ = fs.Parse(args)
	if len(apps) == 0 {
		fail(errors.New("no -app given"), 2)
	}

	s := session()
	defer s.Close()
	removeAll(s) // idempotent: one AGNI set at a time

	if err := s.AddProvider(&wf.Provider{ID: providerID, Name: "AGNI Sovereign Firewall",
		Description: "Keeps AGNI's own processes off the internet", Persistent: true}); err != nil {
		fail(fmt.Errorf("could not add the AGNI provider: %w", err), 4)
	}
	if err := s.AddSublayer(&wf.Sublayer{ID: sublayerID, Name: "AGNI sovereign sublayer",
		Description: "AGNI egress policy", Persistent: true, Provider: providerID,
		Weight: 0x0100}); err != nil {
		fail(fmt.Errorf("could not add the AGNI sublayer: %w", err), 4)
	}

	var confined, skipped []string
	added := 0
	for _, app := range apps {
		if _, err := os.Stat(app); err != nil {
			skipped = append(skipped, app+" (not found)")
			continue
		}
		appID, err := wf.AppID(app)
		if err != nil {
			skipped = append(skipped, fmt.Sprintf("%s (%v)", app, err))
			continue
		}
		for _, layer := range layers {
			add := func(name string, action wf.Action, weight uint64, prefix *netip.Prefix) {
				conds := []*wf.Match{{Field: wf.FieldALEAppID, Op: wf.MatchTypeEqual, Value: appID}}
				if prefix != nil {
					conds = append(conds, &wf.Match{Field: wf.FieldIPRemoteAddress, Op: wf.MatchTypeEqual, Value: *prefix})
				}
				id, gerr := windows.GenerateGUID()
				if gerr != nil {
					fail(gerr, 4)
				}
				rule := &wf.Rule{ID: wf.RuleID(id), Name: "AGNI " + name + " (" + layer.name + ")",
					Description: app, Layer: layer.id, Sublayer: sublayerID, Weight: weight,
					Conditions: conds, Action: action, Persistent: true, Provider: providerID}
				if err := s.AddRule(rule); err != nil {
					fail(fmt.Errorf("could not add filter %q for %s: %w", rule.Name, app, err), 4)
				}
				added++
			}
			for _, p := range loopback {
				p := p
				if p.Addr().Is6() == layer.v6 {
					add("permit loopback", wf.ActionPermit, weightLoopback, &p)
				}
			}
			if *allowLAN {
				for _, p := range lan {
					p := p
					if p.Addr().Is6() == layer.v6 {
						add("permit LAN", wf.ActionPermit, weightLAN, &p)
					}
				}
			}
			add("block", wf.ActionBlock, weightBlock, nil)
		}
		confined = append(confined, app)
	}
	emit(map[string]any{"ok": len(confined) > 0, "filters_added": added, "confined": confined,
		"skipped": skipped, "allow_lan": *allowLAN})
}

func cmdDrops(args []string) {
	fs := flag.NewFlagSet("drops", flag.ExitOnError)
	minutes := fs.Int("minutes", 60, "look back this many minutes")
	_ = fs.Parse(args)
	s := session()
	defer s.Close()
	mine, _, _ := ours(s)
	kernel := map[uint64]string{}
	for _, r := range mine {
		kernel[r.KernelID] = r.Name
	}
	events, err := s.DropEvents()
	if err != nil {
		fail(fmt.Errorf("could not read WFP drop events: %w", err), 3)
	}
	since := time.Now().Add(-time.Duration(*minutes) * time.Minute)
	var list []map[string]any
	for _, e := range events {
		name, ok := kernel[e.FilterID]
		if !ok || e.Timestamp.Before(since) {
			continue
		}
		list = append(list, map[string]any{"time": e.Timestamp.Format(time.RFC3339), "filter": name,
			"remote": e.RemoteAddr.String(), "local": e.LocalAddr.String(), "protocol": e.IPProtocol,
			"app": e.AppID})
	}
	emit(map[string]any{"ok": true, "minutes": *minutes, "blocked": len(list), "events": list})
}

func main() {
	if len(os.Args) > 1 && strings.HasPrefix(os.Args[1], "--out=") {
		outPath = strings.TrimPrefix(os.Args[1], "--out=")
		os.Args = append(os.Args[:1], os.Args[2:]...)
	}
	if len(os.Args) < 2 {
		emit(map[string]any{"ok": false, "error": "usage: agni-wfp status | apply -app PATH [-allow-lan] | remove | drops [-minutes N]"})
		os.Exit(2)
	}
	switch os.Args[1] {
	case "status":
		cmdStatus()
	case "apply":
		cmdApply(os.Args[2:])
	case "remove":
		cmdRemove()
	case "drops":
		cmdDrops(os.Args[2:])
	case "version":
		emit(map[string]any{"ok": true})
	default:
		emit(map[string]any{"ok": false, "error": "unknown command " + os.Args[1]})
		os.Exit(2)
	}
}
