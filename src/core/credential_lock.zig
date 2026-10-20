const std = @import("std");
const builtin = @import("builtin");
const app_runtime = @import("runtime.zig");
const sensitive_file = @import("sensitive_file.zig");

// Serializes publication and native-to-snapshot copies, not network requests.
// A caller holding a per-account refresh lock may acquire this lock; callers
// holding this lock must never acquire a per-account refresh lock.
pub fn acquire(allocator: std.mem.Allocator, codex_home: []const u8) !std.Io.File {
    const accounts = try std.fs.path.join(allocator, &.{ codex_home, "accounts" });
    defer allocator.free(accounts);
    _ = try std.Io.Dir.cwd().createDirPathStatus(app_runtime.io(), accounts, switch (builtin.os.tag) {
        .windows => .default_dir,
        else => .fromMode(0o700),
    });
    const path = try std.fs.path.join(allocator, &.{ accounts, ".credentials.lock" });
    defer allocator.free(path);
    var file = try std.Io.Dir.cwd().createFile(app_runtime.io(), path, .{ .truncate = false, .permissions = sensitive_file.permissions });
    errdefer file.close(app_runtime.io());
    file.lock(app_runtime.io(), .exclusive) catch |err| switch (err) {
        error.FileLocksUnsupported => return error.RefreshLockUnsupported,
        else => return err,
    };
    return file;
}

pub fn release(file: std.Io.File) void {
    file.unlock(app_runtime.io());
    file.close(app_runtime.io());
}
