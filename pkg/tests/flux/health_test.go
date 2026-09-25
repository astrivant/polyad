package flux_test

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/fluxcd/pkg/apis/kustomize"
	"github.com/fluxcd/pkg/runtime/cel"
	"k8s.io/apiextensions-apiserver/pkg/apis/apiextensions"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	"k8s.io/apiextensions-apiserver/pkg/apiserver/schema"
	"k8s.io/apiextensions-apiserver/pkg/apiserver/schema/defaulting"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"sigs.k8s.io/yaml"
)

// readChecks loads exactly the expressions installed by administrators.
func readChecks(t *testing.T) []kustomize.CustomHealthCheck {
	t.Helper()
	data, err := os.ReadFile(os.Getenv("POLYAD_FLUX_CHECKS"))
	if err != nil {
		t.Fatal(err)
	}
	var config struct {
		Spec struct {
			Checks []kustomize.CustomHealthCheck `json:"healthCheckExprs"`
		} `json:"spec"`
	}
	if err := json.Unmarshal(data, &config); err != nil {
		t.Fatal(err)
	}
	return config.Spec.Checks
}

// readSchemas loads the shipped CRDs, not a test-only status default. Kubernetes
// applies these defaults on reads as well as creates, including existing objects.
func readSchemas(t *testing.T) map[string]*schema.Structural {
	t.Helper()
	paths, err := filepath.Glob("../../../charts/polyad-crds/crds/*.yaml")
	if err != nil {
		t.Fatal(err)
	}
	schemas := make(map[string]*schema.Structural)
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var crd apiextensionsv1.CustomResourceDefinition
		if err := yaml.Unmarshal(data, &crd); err != nil {
			t.Fatal(err)
		}
		if crd.Spec.Group != "polyad.astrivant.com" {
			continue
		}
		for _, version := range crd.Spec.Versions {
			var internal apiextensions.JSONSchemaProps
			if err := apiextensionsv1.Convert_v1_JSONSchemaProps_To_apiextensions_JSONSchemaProps(version.Schema.OpenAPIV3Schema, &internal, nil); err != nil {
				t.Fatal(err)
			}
			structural, err := schema.NewStructural(&internal)
			if err != nil {
				t.Fatalf("%s: %v", path, err)
			}
			schemas[crd.Spec.Group+"/"+version.Name+"/"+crd.Spec.Names.Kind] = structural
		}
	}
	return schemas
}

// defaultObject reproduces API-server defaulting without synthesizing observations
// or replacing status fields that an operator already published.
func defaultObject(t *testing.T, obj *unstructured.Unstructured, schemas map[string]*schema.Structural) {
	t.Helper()
	structural, ok := schemas[obj.GetAPIVersion()+"/"+obj.GetKind()]
	if !ok {
		t.Fatalf("no CRD schema for %s/%s", obj.GetAPIVersion(), obj.GetKind())
	}
	defaulting.Default(obj.Object, structural)
}

// TestHealth exercises Flux's real precedence and generation handling on objects
// defaulted with Kubernetes' implementation and our actual CRD schemas.
func TestHealth(t *testing.T) {
	checks, schemas := readChecks(t), readSchemas(t)
	casesData, err := os.ReadFile("cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name            string          `json:"name"`
		Object          json.RawMessage `json:"object"`
		Expected        string          `json:"expected"`
		Error           bool            `json:"error"`
		WithoutDefaults bool            `json:"withoutDefaults"`
	}
	if err := json.Unmarshal(casesData, &cases); err != nil {
		t.Fatal(err)
	}
	for _, tc := range cases {
		t.Run(tc.Name, func(t *testing.T) {
			t.Parallel()
			obj := &unstructured.Unstructured{}
			if err := obj.UnmarshalJSON(tc.Object); err != nil {
				t.Fatal(err)
			}
			if !tc.WithoutDefaults {
				defaultObject(t, obj, schemas)
			}
			for _, check := range checks {
				if check.Kind != obj.GetKind() || check.APIVersion != obj.GetAPIVersion() {
					continue
				}

				// Exercise Flux's real CEL semantics rather than duplicating the expression logic in Python.
				evaluator, err := cel.NewStatusEvaluator(&check.HealthCheckExpressions)
				if err != nil {
					t.Fatal(err)
				}
				result, err := evaluator.Evaluate(context.Background(), obj)
				if tc.Error {
					if err == nil || !strings.Contains(err.Error(), "no such attribute(s): status") {
						t.Fatalf("expected a missing status evaluation error, got result %v, error %v", result, err)
					}
					return
				}
				if err != nil {
					t.Fatal(err)
				}
				if string(result.Status) != tc.Expected {
					t.Fatalf("got %v, want %s", result.Status, tc.Expected)
				}
				return
			}
			t.Fatalf("no health check for %v", obj.GetKind())
		})
	}
}

// TestInitialStatus covers every generated kind before the first operator report.
// A default must not invent readiness, progress, or an observed generation.
func TestInitialStatus(t *testing.T) {
	checks, schemas := readChecks(t), readSchemas(t)
	definitions := map[string]bool{
		"Workload": true, "Daemon": true, "Resource": true,
		"Gate": true, "ShutdownPolicy": true, "GraphPolicy": true,
	}
	for _, check := range checks {
		for _, initial := range []string{"absent", "null", "empty"} {
			for _, template := range []bool{false, true} {
				boundary := check.Kind == "Graph" || check.Kind == "PolyGraph" || check.Kind == "ReplicaGroup"
				if template && !boundary {
					continue
				}
				name := check.Kind + "/" + initial
				if template {
					name += "/template"
				}
				t.Run(name, func(t *testing.T) {
					t.Parallel()
					obj := &unstructured.Unstructured{Object: map[string]any{
						"apiVersion": check.APIVersion, "kind": check.Kind,
						"metadata": map[string]any{"name": "initial", "generation": int64(1)},
						"spec":     map[string]any{},
					}}
					if initial == "null" {
						obj.Object["status"] = nil
					} else if initial == "empty" {
						obj.Object["status"] = map[string]any{}
					}
					if template {
						obj.Object["spec"] = map[string]any{"templateOnly": true}
					}
					defaultObject(t, obj, schemas)
					if check.Kind != "GraphPolicy" {
						initialStatus, ok := obj.Object["status"].(map[string]any)
						if !ok || len(initialStatus) != 0 {
							t.Fatalf("expected an empty default status, got %#v", obj.Object["status"])
						}
					}
					evaluator, err := cel.NewStatusEvaluator(&check.HealthCheckExpressions)
					if err != nil {
						t.Fatal(err)
					}
					result, err := evaluator.Evaluate(context.Background(), obj)
					if err != nil {
						t.Fatal(err)
					}
					want := "InProgress"
					if definitions[check.Kind] || template {
						want = "Current"
					}
					if string(result.Status) != want {
						t.Fatalf("got %v, want %s", result.Status, want)
					}
				})
			}
		}
	}
}
