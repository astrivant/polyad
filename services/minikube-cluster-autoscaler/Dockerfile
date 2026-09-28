# Build only the optional integration, without the Polyad operator or SDK.
FROM golang:1.27.1 AS build
WORKDIR /src
COPY pkg/minikube-cluster-autoscaler/go.mod pkg/minikube-cluster-autoscaler/go.sum ./
RUN go mod download
COPY pkg/minikube-cluster-autoscaler/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /provider .

FROM scratch
COPY --from=build /provider /provider
USER 65532:65532
EXPOSE 50051
ENTRYPOINT ["/provider"]
CMD ["--mode=serve", "--config=/state/config.json", "--state-dir=/state"]
