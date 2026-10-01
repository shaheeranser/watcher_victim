FROM golang:1.23-alpine AS build

WORKDIR /src

COPY go.mod ./
COPY main.go ./

RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/victim-go .

FROM alpine:3.20

COPY --from=build /out/victim-go /usr/local/bin/victim-go

EXPOSE 8081

USER nobody

ENTRYPOINT ["/usr/local/bin/victim-go"]
