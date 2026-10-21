# Go targets (Python->Go, Java->Go). Go 1.21 as in RepoTransBench. Runs are offline: no module proxy, and the
# standard library is pre-compiled into /opt/gocache (copied to a writable cache at run time).
FROM golang:1.21-bookworm
RUN useradd -m -u 1000 sandbox && mkdir -p /opt/gocache && chown sandbox /opt/gocache
USER sandbox
ENV GOCACHE=/opt/gocache GOPATH=/tmp/gopath GOPROXY=off GOFLAGS=-buildvcs=false GOTOOLCHAIN=local \
    CGO_ENABLED=0 GOTELEMETRY=off
RUN cd /tmp && mkdir warm && cd warm && go mod init warm \
 && printf 'package main\nimport (\n"encoding/json"\n"fmt"\n"math/big"\n"os"\n"reflect"\n"sort"\n"strings"\n"errors"\n"testing"\n)\nvar _ = testing.Short\nfunc main() { b, _ := json.Marshal(map[string]any{"a": big.NewInt(1).String()}); fmt.Println(string(b), reflect.TypeOf(0), sort.IntsAreSorted(nil), strings.ToUpper("x"), errors.New("e"), os.Args[0]) }\n' > main.go \
 && go build -o /dev/null . && go vet . && cd / && rm -rf /tmp/warm
WORKDIR /work
