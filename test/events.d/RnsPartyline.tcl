###############################################################################
#
# RnsPartyline module event handlers
#
# This is the namespace used by the module. The name must match the
# configuration variable NAME in [ModuleRnsPartyline].
#
###############################################################################

namespace eval RnsPartyline {

# Load Module core handlers
sourceTclWithOverrides "Module.tcl"
mixin Module

# No custom Tcl handlers for M1.

# end of namespace
}
