/* Declaration-only prototype: cur_func stays unset at file scope, so a
 * non-constant static initializer must become a CompilerError rather than
 * KeyError: None inside ILCode.add. */
int decl_only_f(void);

// error: non-constant initializer for variable with static storage duration
static int decl_only_x = decl_only_f();

int main(void) { return 0; }
