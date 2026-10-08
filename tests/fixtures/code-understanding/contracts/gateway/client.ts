// Original synthetic bindings; café and 雪 exercise physical UTF-8 ranges.
export function ordersHttp(transport: any, value: string): any { return transport.http("orders", "orders-api", "POST", "/echo", "echo", "EchoRequest", "EchoReply", value); }
export function workerHttp(transport: any, value: string): any { return transport.http("worker", "worker-api", "POST", "/echo", "echo", "EchoRequest", "EchoReply", value); }
export function workerRpc(transport: any, value: string): any { return transport.rpc("worker", "fixture.worker", "EchoService", "Echo", "EchoRequest", "EchoReply", value); }
export function missingService(transport: any, value: string): any { return transport.http("", "orders-api", "POST", "/echo", "echo", "EchoRequest", "EchoReply", value); }
export function computedService(transport: any, service: string, value: string): any { return transport.http(service, "orders-api", "POST", "/echo", "echo", "EchoRequest", "EchoReply", value); }
export function computedPath(transport: any, path: string, value: string): any { return transport.http("orders", "orders-api", "POST", path, "echo", "EchoRequest", "EchoReply", value); }
export function missingPath(transport: any, value: string): any { return transport.http("orders", "orders-api", "POST", "", "echo", "EchoRequest", "EchoReply", value); }
export function missingOperation(transport: any, value: string): any { return transport.rpc("worker", "fixture.worker", "EchoService", "Missing", "EchoRequest", "EchoReply", value); }
export function wrongSchema(transport: any, value: string): any { return transport.rpc("worker", "fixture.worker", "EchoService", "Echo", "MissingRequest", "EchoReply", value); }
export function duplicateRpc(transport: any, value: string): any { return transport.rpc("worker", "fixture.worker", "EchoService", "Duplicate", "EchoRequest", "EchoReply", value); }
export function staleRpc(transport: any, value: string): any { return transport.rpc("worker", "fixture.worker", "EchoService", "Echo", "EchoRequest", "EchoReply", value); }
export function generatedRpc(transport: any, value: string): any { return transport.rpc("worker", "fixture.worker", "EchoService", "Echo", "EchoRequest", "EchoReply", value); }
export function lookalike(transport: any, value: string): any { return transport.echo(value); }
