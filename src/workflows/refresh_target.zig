const std = @import("std");
const oauth_refresh = @import("../auth/oauth_refresh.zig");

pub fn refreshForSwitch(allocator: std.mem.Allocator, codex_home: []const u8, account_key: []const u8) !void {
    _ = oauth_refresh.refreshAccount(allocator, codex_home, account_key, false) catch |err| switch (err) {
        error.RefreshLoginRequired, error.RefreshProtocolFailure => return error.LoginRequired,
        error.MissingRefreshToken, error.MissingTokens => return,
        else => return err,
    };
}
