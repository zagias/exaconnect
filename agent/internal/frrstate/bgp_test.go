package frrstate

import (
	"reflect"
	"testing"
)

// Trimmed from FRR 10.2 `show bgp neighbors 169.254.100.1 json`.
const neighborJSON = `{"169.254.100.1":{"remoteAs":64512,"localAs":65000,"nbrExternalLink":true,"bgpVersion":4,
"remoteRouterId":"169.254.100.1","localRouterId":"100.64.1.1","bgpState":"Established","bgpTimerUpMsec":42000,
"bgpTimerUpString":"00:00:42","neighborCapabilities":{"4byteAs":"advertisedAndReceived"},
"addressFamilyInfo":{"ipv4Unicast":{"updateGroupId":2,"subGroupId":2,"packetQueueLength":0,
"inboundPathPolicyConfig":true,"outboundPathPolicyConfig":true,"incomingUpdatePrefixFilterList":"VC7-IN",
"outgoingUpdatePrefixFilterList":"VC7-OUT","acceptedPrefixCounter":2,"sentPrefixCounter":1,
"prefixAllowedMax":100,"prefixAllowedMaxWarning":false}},"connectionsEstablished":1,"connectionsDropped":0}}`

const neighborActive = `{"169.254.100.5":{"remoteAs":12076,"localAs":65000,"bgpState":"Active",
"addressFamilyInfo":{"ipv4Unicast":{"incomingUpdatePrefixFilterList":"VC8-IN"}}}}`

// Trimmed from FRR 10.2 `show bgp ipv4 unicast neighbors 169.254.100.1 routes json`.
const routesJSON = `{"vrfId": 0,"vrfName": "default","tableVersion": 9,"routerId": "100.64.1.1","defaultLocPrf": 100,
"localAS": 65000,"routes": { "10.100.0.0/16": [{"valid":true,"bestpath":true,"pathFrom":"external",
"prefix":"10.100.0.0","prefixLen":16,"network":"10.100.0.0/16","metric":100,"weight":0,"peerId":"169.254.100.1",
"path":"64512","origin":"IGP","nexthops":[{"ip":"169.254.100.1","afi":"ipv4","used":true}]}],
"10.20.0.0/24": [{"valid":true,"bestpath":true,"pathFrom":"external","prefix":"10.20.0.0","prefixLen":24,
"network":"10.20.0.0/24","peerId":"169.254.100.1","path":"64512","origin":"IGP"}],
"10.100.4.0/24": [{"valid":true,"pathFrom":"external","prefix":"10.100.4.0","prefixLen":24}] },
"totalRoutes": 3, "totalPaths": 3}`

func TestParseBGPNeighbor(t *testing.T) {
	n, err := ParseBGPNeighbor([]byte(neighborJSON), "169.254.100.1")
	if err != nil || n.State != "Established" || n.PrefixesReceived != 2 {
		t.Fatalf("%+v %v", n, err)
	}
	n, err = ParseBGPNeighbor([]byte(neighborActive), "169.254.100.5")
	if err != nil || n.State != "Active" || n.PrefixesReceived != 0 {
		t.Fatalf("%+v %v", n, err)
	}
	n, err = ParseBGPNeighbor([]byte(`{"bgpNoSuchNeighbor":true}`), "169.254.100.9")
	if err != nil || n.State != "" {
		t.Fatalf("unknown neighbour: %+v %v", n, err)
	}
	if _, err := ParseBGPNeighbor([]byte(`% Unknown command`), "x"); err == nil {
		t.Fatal("expected a parse error")
	}
}

func TestParseNeighborRoutes(t *testing.T) {
	got, err := ParseNeighborRoutes([]byte(routesJSON), 20)
	want := []string{"10.20.0.0/24", "10.100.0.0/16", "10.100.4.0/24"}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v %v", got, err)
	}
	got, _ = ParseNeighborRoutes([]byte(routesJSON), 2)
	if len(got) != 2 {
		t.Fatalf("limit not applied: %v", got)
	}
	got, err = ParseNeighborRoutes([]byte(`{"vrfId":0,"routes":{}}`), 20)
	if err != nil || got == nil || len(got) != 0 {
		t.Fatalf("empty: %v %v", got, err)
	}
}
