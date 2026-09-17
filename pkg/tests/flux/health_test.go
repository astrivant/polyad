package flux_test

import (
	"context"
	"encoding/json"
	"os"
	"testing"

	"github.com/fluxcd/pkg/apis/kustomize"
	"github.com/fluxcd/pkg/runtime/cel"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

// Test generated configuration with Flux's evaluator, including its precedence
// and generation handling. The Python generator supplies the actual expressions.
func TestHealth(t *testing.T) {
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
	casesData, err := os.ReadFile("cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name     string          `json:"name"`
		Object   json.RawMessage `json:"object"`
		Expected string          `json:"expected"`
		Error    bool            `json:"error"`
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
			for _, check := range config.Spec.Checks {
				if check.Kind != obj.GetKind() {
					continue
				}
				evaluator, err := cel.NewStatusEvaluator(&check.HealthCheckExpressions)
				if err != nil {
					t.Fatal(err)
				}
				result, err := evaluator.Evaluate(context.Background(), obj)
				if tc.Error {
					if err == nil {
						t.Fatalf("expected a missing status evaluation error, got %v", result)
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
