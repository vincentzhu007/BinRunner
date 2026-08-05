// 测试用二进制：打印参数、环境信息，验证 stdout/stderr/退出码都被正确捕获。
// 编译（见 tools/build_hello.sh）后重命名为 libhello.so 放入 entry/libs/arm64-v8a/
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char *argv[])
{
    printf("hello from bundled binary!\n");
    printf("argc=%d\n", argc);
    for (int i = 0; i < argc; i++) {
        printf("argv[%d] = %s\n", i, argv[i]);
    }
    printf("pid=%d uid=%d\n", getpid(), getuid());
    fprintf(stderr, "this line goes to stderr\n");
    return 42; // 故意返回非 0，验证退出码透传
}
