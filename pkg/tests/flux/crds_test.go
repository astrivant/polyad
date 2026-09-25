package flux_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"k8s.io/apiextensions-apiserver/pkg/apis/apiextensions"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	"k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/validation"
	"k8s.io/apiextensions-apiserver/pkg/apiserver/schema"
	schemacel "k8s.io/apiextensions-apiserver/pkg/apiserver/schema/cel"
	"k8s.io/apimachinery/pkg/util/validation/field"
	celconfig "k8s.io/apiserver/pkg/apis/cel"
	"sigs.k8s.io/yaml"
)

// TestCRDAdmission runs Kubernetes' definition validator, including structural
// schema, defaults and CEL checks that a JSON Schema validator cannot enforce.
func TestCRDAdmission(t *testing.T) {
	paths, err := filepath.Glob("../../../charts/polyad-crds/crds/*.yaml")
	if err != nil || len(paths) == 0 {
		t.Fatalf("cannot locate shipped CRDs: %v", err)
	}
	for _, path := range paths {
		t.Run(filepath.Base(path), func(t *testing.T) {
			t.Parallel()
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			var crd apiextensionsv1.CustomResourceDefinition
			if err := yaml.UnmarshalStrict(data, &crd); err != nil {
				t.Fatal(err)
			}

			// API decoding installs these defaults before admission. Converting the
			// whole definition also matches Kubernetes' internal version handling.
			apiextensionsv1.SetObjectDefaults_CustomResourceDefinition(&crd)
			var internal apiextensions.CustomResourceDefinition
			if err := apiextensionsv1.Convert_v1_CustomResourceDefinition_To_apiextensions_CustomResourceDefinition(&crd, &internal, nil); err != nil {
				t.Fatal(err)
			}
			for _, err := range validation.ValidateCustomResourceDefinition(context.Background(), &internal) {
				t.Error(err)
			}
		})
	}
}

// checkCEL exercises field rules on real values, including nullable fields that
// Kubernetes exposes as absent rather than comparable typed nulls in CEL.
func checkCEL(t *testing.T, structural schema.Structural, object map[string]any, valid bool) {
	t.Helper()
	validator := schemacel.NewValidator(&structural, false, celconfig.PerCallLimit)
	if validator == nil {
		t.Fatal("expected validation rules")
	}
	errs, _ := validator.Validate(context.Background(), field.NewPath("spec"), &structural, object, nil, celconfig.RuntimeCELCostBudget)
	if (len(errs) == 0) != valid {
		t.Fatalf("valid=%v for %v: %v", valid, object, errs)
	}
}

// TestThroughputBoundsCEL keeps absent/null bounds and real numeric intervals
// consistent across all three CRDs generated from the same throughput model.
func TestThroughputBoundsCEL(t *testing.T) {
	schemas := readSchemas(t)
	for _, kind := range []string{"Graph", "PolyGraph", "Rewrite"} {
		spec := schemas["polyad.astrivant.com/v1alpha1/"+kind].Properties["spec"]
		if kind == "Rewrite" {
			spec = spec.Properties["topology"]
		}
		bounds := spec.Properties["throughput"].Properties["tiers"].Items.Properties["cheeger"]
		for _, tc := range []struct {
			name   string
			bounds map[string]any
			valid  bool
		}{
			{"missing", map[string]any{}, false},
			{"null", map[string]any{"minimum": nil, "maximum": nil}, false},
			{"minimum", map[string]any{"minimum": float64(0)}, true},
			{"maximum", map[string]any{"maximum": float64(1)}, true},
			{"nullable", map[string]any{"minimum": nil, "maximum": float64(1)}, true},
			{"interval", map[string]any{"minimum": float64(0), "maximum": float64(1)}, true},
			{"equal", map[string]any{"minimum": float64(1), "maximum": float64(1)}, true},
			{"reversed", map[string]any{"minimum": float64(2), "maximum": float64(1)}, false},
		} {
			t.Run(kind+"/"+tc.name, func(t *testing.T) {
				checkCEL(t, bounds, tc.bounds, tc.valid)
			})
		}
	}
}

// TestDaemonControllerCEL ensures a repeated YAML key cannot silently discard
// DaemonSet constraints while leaving StatefulSet checks in place.
func TestDaemonControllerCEL(t *testing.T) {
	spec := readSchemas(t)["polyad.astrivant.com/v1alpha1/Daemon"].Properties["spec"]
	for _, tc := range []struct {
		name  string
		spec  map[string]any
		valid bool
	}{
		{"deployment", map[string]any{}, true},
		{"daemonset", map[string]any{"controller": "DaemonSet"}, true},
		{"daemonset-replicas", map[string]any{"controller": "DaemonSet", "replicas": int64(2)}, false},
		{"daemonset-activation", map[string]any{"controller": "DaemonSet", "activation": map[string]any{}}, false},
		{"daemonset-stateful", map[string]any{"controller": "DaemonSet", "statefulSet": map[string]any{}}, false},
		{"stateful-missing-options", map[string]any{"controller": "StatefulSet"}, false},
		{"stateful", map[string]any{"controller": "StatefulSet", "statefulSet": map[string]any{"serviceName": "worker"}}, true},
		{"options-without-controller", map[string]any{"statefulSet": map[string]any{}}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			checkCEL(t, spec, tc.spec, tc.valid)
		})
	}
}
