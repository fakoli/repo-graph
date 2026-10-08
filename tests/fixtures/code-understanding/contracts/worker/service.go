// Original synthetic bindings; café and 雪 exercise physical UTF-8 ranges.
package worker

func HTTPEcho(value string) string { return value }
func RPCEcho(value string) string { return value }
func ConsumeCreated(value string) string { return value }
func DuplicateA(value string) string { return value }
func DuplicateB(value string) string { return value }
func MissingRPC(value string) string { return value }

var HTTPBinding = map[string]string{"service": "worker", "namespace": "worker-api", "method": "POST", "path": "/echo", "operation": "echo", "request": "EchoRequest", "response": "EchoReply", "handler": "HTTPEcho"}
var RPCBinding = map[string]string{"service": "worker", "namespace": "fixture.worker", "rpc_service": "EchoService", "operation": "Echo", "request": "EchoRequest", "response": "EchoReply", "handler": "RPCEcho"}
var QueueBinding = map[string]string{"service": "worker", "contract_service": "orders", "namespace": "orders.events", "topic": "created", "schema": "OrderCreated", "handler": "ConsumeCreated"}
var DuplicateBindingA = map[string]string{"service": "worker", "namespace": "fixture.worker", "rpc_service": "EchoService", "operation": "Duplicate", "request": "EchoRequest", "response": "EchoReply", "handler": "DuplicateA"}
var DuplicateBindingB = map[string]string{"service": "worker", "namespace": "fixture.worker", "rpc_service": "EchoService", "operation": "Duplicate", "request": "EchoRequest", "response": "EchoReply", "handler": "DuplicateB"}
