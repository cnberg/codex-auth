const std = @import("std");
const builtin = @import("builtin");
const app_runtime = @import("runtime.zig");

pub const permissions: std.Io.File.Permissions = switch (builtin.os.tag) {
    .windows => .default_file,
    else => .fromMode(0o600),
};

pub fn writeAtomic(path: []const u8, data: []const u8) !void {
    var buffer: [4096]u8 = undefined;
    var atomic = try std.Io.Dir.cwd().createFileAtomic(app_runtime.io(), path, .{
        .replace = true,
        .permissions = permissions,
    });
    defer atomic.deinit(app_runtime.io());
    var writer = atomic.file.writer(app_runtime.io(), &buffer);
    try writer.interface.writeAll(data);
    try writer.interface.flush();
    try atomic.file.sync(app_runtime.io());
    try atomic.replace(app_runtime.io());
    try harden(path);
}

fn harden(path: []const u8) !void {
    if (builtin.os.tag == .windows) return;
    const file = try std.Io.Dir.cwd().openFile(app_runtime.io(), path, .{ .mode = .read_write });
    defer file.close(app_runtime.io());
    try file.setPermissions(app_runtime.io(), permissions);
}
