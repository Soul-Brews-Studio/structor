package oauth

import (
	"crypto/sha256"
	"encoding/base64"
	"testing"
)

func TestPKCE(t *testing.T) {
	verifier := "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
	sum := sha256.Sum256([]byte(verifier))
	challenge := base64.RawURLEncoding.EncodeToString(sum[:])
	if !pkceOK(challenge, verifier) {
		t.Fatal("valid verifier rejected")
	}
	if pkceOK(challenge, verifier+"x") {
		t.Fatal("wrong verifier accepted")
	}
	if pkceOK("", verifier) || pkceOK(challenge, "") {
		t.Fatal("empty values accepted")
	}
}

func TestRandomTokenUnique(t *testing.T) {
	a, b := randomToken(32), randomToken(32)
	if a == b || len(a) < 40 {
		t.Fatalf("tokens not random enough: %s %s", a, b)
	}
	if hashToken(a) == hashToken(b) || len(hashToken(a)) != 64 {
		t.Fatal("hash broken")
	}
}
