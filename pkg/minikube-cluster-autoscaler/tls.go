package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"time"
)

func certificate(dir, name string, template, parent *x509.Certificate, parentKey *ecdsa.PrivateKey) (*ecdsa.PrivateKey, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, err
	}
	if parentKey == nil {
		parentKey = key
		parent = template
	}
	der, err := x509.CreateCertificate(rand.Reader, template, parent, &key.PublicKey, parentKey)
	if err != nil {
		return nil, nil, err
	}
	encoded, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, nil, err
	}
	if err = os.MkdirAll(dir, 0700); err != nil {
		return nil, nil, err
	}
	if err = os.WriteFile(filepath.Join(dir, name+".key"), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: encoded}), 0600); err != nil {
		return nil, nil, err
	}
	cert := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	err = os.WriteFile(filepath.Join(dir, name+".crt"), cert, 0600)
	return key, cert, err
}

func issuePKI(dir, host string) error {
	now := time.Now()
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "Polyad local autoscaler CA"},
		NotBefore: now.Add(-time.Minute), NotAfter: now.Add(365 * 24 * time.Hour), IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	key, caPEM, err := certificate(filepath.Join(dir, "authority"), "ca", ca, nil, nil)
	if err != nil {
		return err
	}
	for i, spec := range []struct {
		directory, name string
		server          bool
	}{
		{"provider", "provider", true}, {"provider", "bridge-client", false}, {"host", "bridge", true}, {"client", "autoscaler-client", false},
	} {
		leaf := &x509.Certificate{SerialNumber: big.NewInt(int64(i + 2)), Subject: pkix.Name{CommonName: spec.name},
			NotBefore: ca.NotBefore, NotAfter: ca.NotAfter, KeyUsage: x509.KeyUsageDigitalSignature}
		if spec.server {
			leaf.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
			leaf.IPAddresses = []net.IP{net.ParseIP(host)}
			leaf.DNSNames = []string{"host.docker.internal"}
		} else {
			leaf.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
		}
		destination := filepath.Join(dir, spec.directory, "tls")
		if _, _, err = certificate(destination, spec.name, leaf, ca, key); err != nil {
			return err
		}
		if err = os.WriteFile(filepath.Join(destination, "ca.crt"), caPEM, 0600); err != nil {
			return err
		}
	}
	return nil
}

func trust(dir, name string) (tls.Certificate, *x509.CertPool, error) {
	cert, err := tls.LoadX509KeyPair(filepath.Join(dir, name+".crt"), filepath.Join(dir, name+".key"))
	if err != nil {
		return cert, nil, err
	}
	data, err := os.ReadFile(filepath.Join(dir, "ca.crt"))
	if err != nil {
		return cert, nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(data) {
		return cert, nil, fmt.Errorf("invalid CA")
	}
	return cert, pool, nil
}

func clientTLS(dir, name string) (*tls.Config, error) {
	cert, pool, err := trust(dir, name)
	if err != nil {
		return nil, err
	}
	return &tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{cert}, RootCAs: pool}, nil
}

func serverTLS(dir, name, client string) (*tls.Config, error) {
	cert, pool, err := trust(dir, name)
	if err != nil {
		return nil, err
	}
	return &tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{cert}, ClientCAs: pool,
		ClientAuth: tls.RequireAndVerifyClientCert, VerifyConnection: func(c tls.ConnectionState) error {
			if len(c.PeerCertificates) == 0 || c.PeerCertificates[0].Subject.CommonName != client {
				return fmt.Errorf("wrong client role")
			}
			return nil
		}}, nil
}
