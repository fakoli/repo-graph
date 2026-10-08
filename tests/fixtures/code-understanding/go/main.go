// Synthetic source only: café, λ and 🌱 test UTF-8 offsets.
package fixture

import (
	alias "example.test/codeunderstanding/helpers"
	missing "example.test/codeunderstanding/optional"
)

func Local(value int) int      { return value + 1 }
func Direct() int              { return Local(1) }
func Imported() int            { return alias.Finish(2) }
func Reference() func(int) int { return Local }
func ValueAlias() int {
	nextStep := Local
	return nextStep(2)
}
func Shadow() int {
	Local := func(value int) int { return value + 100 }
	return Local(3)
}
func Callback(fn func(int) int) int { return fn(4) }

type Worker interface{ Run() int }
type First struct{}
type Second struct{}

func (First) Run() int  { return Local(5) }
func (Second) Run() int { return alias.Finish(6) }
func Receiver(chooseFirst bool) int {
	var worker Worker = First{}
	if !chooseFirst {
		worker = Second{}
	}
	return worker.Run()
}
func Missing() int                                            { return missing.Added(7) }
func Dynamic(table map[string]func(int) int, name string) int { return table[name](8) }
func café(value int) int                                      { return value - 1 }
func UnicodeCall() int                                        { return café(9) }
