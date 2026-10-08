"""Original synthetic bindings; café and 雪 exercise physical UTF-8 ranges."""


def echo(value):
    return value


HTTP_BINDING = {"service": "orders", "namespace": "orders-api", "method": "POST", "path": "/echo", "operation": "echo", "request": "EchoRequest", "response": "EchoReply", "handler": echo}


def publish_created(transport, value):
    return transport.publish("orders", "orders.events", "created", "OrderCreated", value)


def publish_computed(transport, topic, value):
    return transport.publish("orders", "orders.events", topic, "OrderCreated", value)


def publish_absent(transport, value):
    return transport.publish("orders", "orders.events", "absent", "OrderCreated", value)


def publish_wrong_namespace(transport, value):
    return transport.publish("orders", "worker.events", "created", "OrderCreated", value)
