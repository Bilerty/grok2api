package egress

import "testing"

func TestProxyIdentityFromURL(t *testing.T) {
	cases := []struct {
		name string
		url  string
		want string
	}{
		{"placeholder_identity", "https://platformA.{account}:token@host:443", "platformA.{account}"},
		{"real_account_normalized", "https://resin.p1.user123:pw@host:2260", "resin.p1.{account}"},
		{"multi_dot_platform", "https://resin.gw.pool.acc-0007:pw@host:2260", "resin.gw.pool.{account}"},
		{"no_dot_username", "http://plainuser:pw@host:8080", "plainuser"},
		{"empty_url", "", ""},
		{"invalid_url", "://bad", ""},
		{"no_userinfo", "http://host:8080", ""},
		{"trailing_dot", "http://platform.:pw@host:8080", "platform."},
		{"socks_scheme", "socks5://pool.{email}:pw@host:1080", "pool.{account}"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ProxyIdentityFromURL(tc.url); got != tc.want {
				t.Fatalf("ProxyIdentityFromURL(%q) = %q, want %q", tc.url, got, tc.want)
			}
		})
	}
}
