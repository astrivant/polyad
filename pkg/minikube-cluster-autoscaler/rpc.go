package main

import (
	"context"
	"fmt"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	pb "polyad.local/minikube-autoscaler/internal/protos"
)

type rpcServer struct {
	pb.UnimplementedCloudProviderServer
	p *provider
}

func group(st State) *pb.NodeGroup {
	return &pb.NodeGroup{Id: groupID, MinSize: int32(st.Config.MinWorkers), MaxSize: int32(st.Config.MaxWorkers), Debug: "Minikube elastic workers; existing VMs are protected"}
}

func validGroup(id string) error {
	if id != groupID {
		return status.Error(codes.NotFound, "unknown node group")
	}
	return nil
}

func rpcError(err error) error {
	if err == nil {
		return nil
	}
	return status.Error(codes.FailedPrecondition, err.Error())
}

func (r *rpcServer) NodeGroups(context.Context, *pb.NodeGroupsRequest) (*pb.NodeGroupsResponse, error) {
	r.p.mu.Lock()
	defer r.p.mu.Unlock()
	return &pb.NodeGroupsResponse{NodeGroups: []*pb.NodeGroup{group(r.p.state)}}, nil
}

func (r *rpcServer) NodeGroupForNode(_ context.Context, q *pb.NodeGroupForNodeRequest) (*pb.NodeGroupForNodeResponse, error) {
	r.p.mu.Lock()
	defer r.p.mu.Unlock()
	for _, w := range r.p.state.Workers {
		if q.Node != nil && w.ID == q.Node.ProviderID && (w.Name == q.Node.Name || q.Node.Name == w.ID) {
			return &pb.NodeGroupForNodeResponse{NodeGroup: group(r.p.state)}, nil
		}
	}
	return &pb.NodeGroupForNodeResponse{NodeGroup: &pb.NodeGroup{}}, nil
}

func (r *rpcServer) GPULabel(context.Context, *pb.GPULabelRequest) (*pb.GPULabelResponse, error) {
	return &pb.GPULabelResponse{}, nil
}
func (r *rpcServer) GetAvailableGPUTypes(context.Context, *pb.GetAvailableGPUTypesRequest) (*pb.GetAvailableGPUTypesResponse, error) {
	return &pb.GetAvailableGPUTypesResponse{}, nil
}
func (r *rpcServer) Cleanup(context.Context, *pb.CleanupRequest) (*pb.CleanupResponse, error) {
	return &pb.CleanupResponse{}, nil
}
func (r *rpcServer) Refresh(context.Context, *pb.RefreshRequest) (*pb.RefreshResponse, error) {
	r.p.mu.Lock()
	defer r.p.mu.Unlock()
	if r.p.state.Error != "" {
		return nil, status.Error(codes.Unavailable, r.p.state.Error)
	}
	return &pb.RefreshResponse{}, nil
}

func (r *rpcServer) NodeGroupTargetSize(_ context.Context, q *pb.NodeGroupTargetSizeRequest) (*pb.NodeGroupTargetSizeResponse, error) {
	if err := validGroup(q.Id); err != nil {
		return nil, err
	}
	r.p.mu.Lock()
	defer r.p.mu.Unlock()
	return &pb.NodeGroupTargetSizeResponse{TargetSize: int32(r.p.state.target())}, nil
}

func (r *rpcServer) NodeGroupIncreaseSize(_ context.Context, q *pb.NodeGroupIncreaseSizeRequest) (*pb.NodeGroupIncreaseSizeResponse, error) {
	if err := validGroup(q.Id); err != nil {
		return nil, err
	}
	return &pb.NodeGroupIncreaseSizeResponse{}, rpcError(r.p.increase(int(q.Delta)))
}

func (r *rpcServer) NodeGroupDecreaseTargetSize(_ context.Context, q *pb.NodeGroupDecreaseTargetSizeRequest) (*pb.NodeGroupDecreaseTargetSizeResponse, error) {
	if err := validGroup(q.Id); err != nil {
		return nil, err
	}
	return &pb.NodeGroupDecreaseTargetSizeResponse{}, rpcError(r.p.decrease(int(q.Delta)))
}

func (r *rpcServer) NodeGroupDeleteNodes(_ context.Context, q *pb.NodeGroupDeleteNodesRequest) (*pb.NodeGroupDeleteNodesResponse, error) {
	if err := validGroup(q.Id); err != nil {
		return nil, err
	}
	r.p.mu.Lock()
	defer r.p.mu.Unlock()
	st := clone(r.p.state)
	if st.Error != "" || len(q.Nodes) == 0 {
		return nil, status.Error(codes.FailedPrecondition, "provider paused or empty deletion request")
	}
	seen := map[string]bool{}
	for _, node := range q.Nodes {
		if node == nil || seen[node.ProviderID] {
			return nil, status.Error(codes.InvalidArgument, "nil or duplicate node")
		}
		seen[node.ProviderID] = true
		found := false
		for i, w := range st.Workers {
			if w.ID != node.ProviderID || w.Name != node.Name {
				continue
			}
			if _, base := st.Base[w.Name]; base || (w.Phase != "ready" && w.Phase != "deleting") {
				break
			}
			st.Workers[i].Phase = "deleting"
			found = true
		}
		if !found {
			return nil, status.Error(codes.FailedPrecondition, "node is not an owned, provisioned worker")
		}
	}
	if st.target() < st.Config.MinWorkers {
		return nil, status.Error(codes.FailedPrecondition, "deletion would violate minimum workers")
	}
	return &pb.NodeGroupDeleteNodesResponse{}, rpcError(r.p.commit(st))
}

func (r *rpcServer) NodeGroupNodes(_ context.Context, q *pb.NodeGroupNodesRequest) (*pb.NodeGroupNodesResponse, error) {
	if err := validGroup(q.Id); err != nil {
		return nil, err
	}
	r.p.mu.Lock()
	defer r.p.mu.Unlock()
	response := &pb.NodeGroupNodesResponse{}
	for _, w := range r.p.state.Workers {
		state := pb.InstanceStatus_instanceCreating
		if w.Phase == "ready" {
			state = pb.InstanceStatus_instanceRunning
		}
		if w.Phase == "deleting" {
			state = pb.InstanceStatus_instanceDeleting
		}
		response.Instances = append(response.Instances, &pb.Instance{Id: w.ID, Status: &pb.InstanceStatus{InstanceState: state}})
	}
	return response, nil
}

func template(st State) v1.Node {
	// Copy observed allocatable resources, not the VM's nominal RAM. Strip host
	// identity, addresses, PodCIDRs, control-plane taints and incidental labels.
	return v1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: "polyad-elastic-template", Labels: map[string]string{
			"kubernetes.io/hostname": "polyad-elastic-template",
			"kubernetes.io/os":       st.Template.Labels["kubernetes.io/os"],
			"kubernetes.io/arch":     st.Template.Labels["kubernetes.io/arch"], poolLabel: "elastic",
		}},
		Spec: v1.NodeSpec{Taints: []v1.Taint{{Key: elasticTaint, Value: "true", Effect: v1.TaintEffectNoSchedule}}},
		Status: v1.NodeStatus{Capacity: st.Template.Status.Capacity.DeepCopy(), Allocatable: st.Template.Status.Allocatable.DeepCopy(),
			Conditions: []v1.NodeCondition{{Type: v1.NodeReady, Status: v1.ConditionTrue}}},
	}
}

func (r *rpcServer) NodeGroupTemplateNodeInfo(_ context.Context, q *pb.NodeGroupTemplateNodeInfoRequest) (*pb.NodeGroupTemplateNodeInfoResponse, error) {
	if err := validGroup(q.Id); err != nil {
		return nil, err
	}
	r.p.mu.Lock()
	defer r.p.mu.Unlock()
	n := template(r.p.state)
	data, err := n.Marshal()
	if err != nil {
		return nil, status.Error(codes.Internal, fmt.Sprint(err))
	}
	return &pb.NodeGroupTemplateNodeInfoResponse{NodeBytes: data}, nil
}
