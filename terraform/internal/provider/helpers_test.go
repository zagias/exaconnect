package provider

import "testing"

func TestNormalisersMatchTheAPI(t *testing.T) {
	for in, want := range map[string]string{
		"10.100.1.0/16": "10.100.0.0/16",
		"203.0.113.5":   "203.0.113.5/32",
		" 10.0.0.0/8 ":  "10.0.0.0/8",
		"not-a-subnet":  "not-a-subnet/32",
	} {
		if got := normCIDR(in); got != want {
			t.Errorf("normCIDR(%q) = %q, want %q", in, got, want)
		}
	}
	for in, want := range map[string]string{
		"Teams.Microsoft.com":            "teams.microsoft.com",
		"https://example.org/path":       "example.org",
		"*.zoom.us":                      "zoom.us",
		"example.com.":                   "example.com",
		"  HTTP://Sub.Example.COM/x/y  ": "sub.example.com",
	} {
		if got := normDomain(in); got != want {
			t.Errorf("normDomain(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestInsidePair(t *testing.T) {
	ours, cloud, ok := insidePair("169.254.100.4/30")
	if !ok || ours != "169.254.100.6/30" || cloud != "169.254.100.5" {
		t.Errorf("insidePair = %q %q %v", ours, cloud, ok)
	}
	if _, _, ok := insidePair("nonsense"); ok {
		t.Error("nonsense should not parse")
	}
}

func TestSplitImportID(t *testing.T) {
	c, id, err := splitImportID("12a21e21-f502-414e-9a85-aa34ffaca0e3/7", "id")
	if err != nil || c != "12a21e21-f502-414e-9a85-aa34ffaca0e3" || id != "7" {
		t.Errorf("got %q %q %v", c, id, err)
	}
	for _, bad := range []string{"7", "/7", "c/", "a/b/c", ""} {
		if _, _, err := splitImportID(bad, "id"); err == nil {
			t.Errorf("splitImportID(%q) should fail", bad)
		}
	}
}
