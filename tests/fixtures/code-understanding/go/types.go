package fixture

type Value int

func Typed(value Value) Value { return value }
func TypedUse(value Value) Value { return Typed(value) }
