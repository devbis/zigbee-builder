#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>

static int cmd_hello(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	shell_print(sh, "nRF52840 USB shell is working; uptime %lld ms",
		    (long long)k_uptime_get());
	return 0;
}

SHELL_CMD_REGISTER(hello, NULL, "Check the shell and uptime", cmd_hello);

int main(void)
{
	return 0;
}
