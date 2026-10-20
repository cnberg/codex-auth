const std = @import("std");
const app_runtime = @import("../core/runtime.zig");
const auth = @import("auth.zig");
const http = @import("../api/http.zig");
const registry = @import("../registry/root.zig");
const sensitive_file = @import("../core/sensitive_file.zig");
const timestamps = @import("../core/timestamp.zig");

pub const token_endpoint = "https://auth.openai.com/oauth/token";
pub const client_id = "app_EMoamEEZ73f0CkXaXp7hrann";
pub const refresh_window_seconds: i64 = 5 * 60;
pub const fallback_max_age_seconds: i64 = 8 * 24 * 60 * 60;

pub const FailureClass = enum { permanent, protocol, transient };
pub const Outcome = enum { fresh, refreshed, reused };

const RefreshState = struct {
    source_refresh_hash: []const u8,
    auth: ?[]const u8 = null,
};

pub fn classifyFailure(status: ?u16, body: []const u8) FailureClass {
    if (hasPermanentErrorCode(body)) return .permanent;
    const value = status orelse return .transient;
    if (value == 429 or value >= 500) return .transient;
    if (value == 400 or value == 401 or value == 403) return .protocol;
    return if (value >= 400) .protocol else .transient;
}

fn hasPermanentErrorCode(body: []const u8) bool {
    var parsed = std.json.parseFromSlice(std.json.Value, std.heap.page_allocator, body, .{}) catch return false;
    defer parsed.deinit();
    const obj = switch (parsed.value) {
        .object => |value| value,
        else => return false,
    };
    if (obj.get("error")) |value| switch (value) {
        .string => |text| return isPermanentCode(text),
        .object => |nested| if (nested.get("code")) |code| switch (code) {
            .string => |text| return isPermanentCode(text),
            else => {},
        },
        else => {},
    };
    if (obj.get("code")) |value| switch (value) {
        .string => |text| return isPermanentCode(text),
        else => {},
    };
    return false;
}

fn isPermanentCode(value: []const u8) bool {
    return std.mem.eql(u8, value, "invalid_grant") or
        std.mem.eql(u8, value, "refresh_token_expired") or
        std.mem.eql(u8, value, "refresh_token_reused") or
        std.mem.eql(u8, value, "refresh_token_invalidated");
}

pub fn shouldRefreshAuthData(allocator: std.mem.Allocator, data: []const u8, now_seconds: i64) bool {
    var parsed = std.json.parseFromSlice(std.json.Value, allocator, data, .{}) catch return false;
    defer parsed.deinit();
    const obj = switch (parsed.value) {
        .object => |value| value,
        else => return false,
    };
    const tokens = switch (obj.get("tokens") orelse return false) {
        .object => |value| value,
        else => return false,
    };
    const access = switch (tokens.get("access_token") orelse return fallbackStale(obj, now_seconds)) {
        .string => |value| value,
        else => return fallbackStale(obj, now_seconds),
    };
    const payload = auth.decodeJwtPayload(allocator, access) catch return fallbackStale(obj, now_seconds);
    defer allocator.free(payload);
    var claims = std.json.parseFromSlice(std.json.Value, allocator, payload, .{}) catch return fallbackStale(obj, now_seconds);
    defer claims.deinit();
    const claims_obj = switch (claims.value) {
        .object => |value| value,
        else => return fallbackStale(obj, now_seconds),
    };
    const exp = switch (claims_obj.get("exp") orelse return fallbackStale(obj, now_seconds)) {
        .integer => |value| value,
        .float => |value| blk: {
            if (!std.math.isFinite(value) or value < 0 or value >= 9_223_372_036_854_775_808.0) return fallbackStale(obj, now_seconds);
            break :blk @as(i64, @intFromFloat(value));
        },
        else => return fallbackStale(obj, now_seconds),
    };
    return exp <= now_seconds +| refresh_window_seconds;
}

fn fallbackStale(obj: std.json.ObjectMap, now_seconds: i64) bool {
    const value = obj.get("last_refresh") orelse return true;
    const text = switch (value) {
        .string => |s| s,
        else => return true,
    };
    const refreshed = @divFloor(timestamps.parseTimestampMs(text) orelse return true, 1000);
    return now_seconds - refreshed >= fallback_max_age_seconds;
}

pub fn refreshAccount(allocator: std.mem.Allocator, codex_home: []const u8, account_key: []const u8, active: bool, force: bool) !Outcome {
    return refreshAccountWith(
        allocator,
        codex_home,
        account_key,
        active,
        force,
        std.Io.Timestamp.now(app_runtime.io(), .real).toSeconds(),
        http.runPostJsonCommand,
        lockExclusive,
    );
}

pub fn refreshAccountWith(
    allocator: std.mem.Allocator,
    codex_home: []const u8,
    account_key: []const u8,
    active: bool,
    force: bool,
    now: i64,
    requester: anytype,
    locker: anytype,
) !Outcome {
    try registry.ensureAccountsDir(allocator, codex_home);
    const file_key = try registry.accountFileKey(allocator, account_key);
    defer allocator.free(file_key);
    const lock_name = try std.fmt.allocPrint(allocator, "{s}.refresh.lock", .{file_key});
    defer allocator.free(lock_name);
    const lock_path = try std.fs.path.join(allocator, &.{ codex_home, "accounts", lock_name });
    defer allocator.free(lock_path);
    const snapshot_path = try registry.accountAuthPath(allocator, codex_home, account_key);
    defer allocator.free(snapshot_path);
    const observed = try readFile(allocator, snapshot_path);
    defer allocator.free(observed);
    var lock_file = try std.Io.Dir.cwd().createFile(app_runtime.io(), lock_path, .{ .truncate = false, .permissions = sensitive_file.permissions });
    defer lock_file.close(app_runtime.io());
    locker(lock_file) catch |err| switch (err) {
        error.FileLocksUnsupported => return error.RefreshLockUnsupported,
        else => return err,
    };
    defer lock_file.unlock(app_runtime.io());

    const state_path = try refreshStatePath(allocator, codex_home, account_key);
    defer allocator.free(state_path);
    if (try recoverState(allocator, codex_home, account_key, snapshot_path, state_path)) return .reused;
    const before = try readFile(allocator, snapshot_path);
    defer allocator.free(before);
    if (!std.mem.eql(u8, observed, before)) return .reused;
    if (!force and !shouldRefreshAuthData(allocator, before, now)) return .fresh;

    const source_hash = try refreshHash(allocator, before);
    // A pending marker survives an ambiguous response or a crash after the POST.
    // Reusing that credential automatically could spend the same token twice.
    try saveState(allocator, state_path, .{ .source_refresh_hash = &source_hash });
    const updated = requestAndApply(allocator, before, now, requester) catch |err| {
        switch (err) {
            error.RefreshLoginRequired, error.CurlRequired, error.MissingRefreshToken, error.MissingTokens => try clearState(state_path),
            else => {},
        }
        return err;
    };
    defer allocator.free(updated);
    var storage_failed = false;
    saveState(allocator, state_path, .{ .source_refresh_hash = &source_hash, .auth = updated }) catch {
        storage_failed = true;
    };
    if (active and try activeAuthMatchesAccount(allocator, codex_home, account_key)) {
        const active_path = try registry.activeAuthPath(allocator, codex_home);
        defer allocator.free(active_path);
        // Preserve a newer credential written by native Codex during the POST.
        const current = try readFile(allocator, active_path);
        defer allocator.free(current);
        const current_hash = try refreshHash(allocator, current);
        if (std.mem.eql(u8, &current_hash, &source_hash)) {
            sensitive_file.writeAtomic(active_path, updated) catch {
                storage_failed = true;
            };
        }
    }
    sensitive_file.writeAtomic(snapshot_path, updated) catch {
        storage_failed = true;
    };
    if (storage_failed) return error.RefreshPersistence;
    try clearState(state_path);
    return .refreshed;
}

pub fn recoverPendingAccounts(allocator: std.mem.Allocator, codex_home: []const u8, reg: *const registry.Registry) !void {
    for (reg.accounts.items) |account| {
        const state_path = try refreshStatePath(allocator, codex_home, account.account_key);
        defer allocator.free(state_path);
        std.Io.Dir.cwd().access(app_runtime.io(), state_path, .{}) catch |err| switch (err) {
            error.FileNotFound => continue,
            else => return err,
        };
        const file_key = try registry.accountFileKey(allocator, account.account_key);
        defer allocator.free(file_key);
        const lock_path = try std.fmt.allocPrint(allocator, "{s}/accounts/{s}.refresh.lock", .{ codex_home, file_key });
        defer allocator.free(lock_path);
        var lock_file = try std.Io.Dir.cwd().createFile(app_runtime.io(), lock_path, .{ .truncate = false, .permissions = sensitive_file.permissions });
        defer lock_file.close(app_runtime.io());
        try lockExclusive(lock_file);
        defer lock_file.unlock(app_runtime.io());
        const snapshot_path = try registry.accountAuthPath(allocator, codex_home, account.account_key);
        defer allocator.free(snapshot_path);
        // In-flight/uncertain requests have no response to recover. The refresh
        // path reports them per account without blocking unrelated accounts.
        _ = recoverState(allocator, codex_home, account.account_key, snapshot_path, state_path) catch |err| switch (err) {
            error.RefreshUncertain => continue,
            else => return err,
        };
    }
}

fn refreshStatePath(allocator: std.mem.Allocator, codex_home: []const u8, account_key: []const u8) ![]u8 {
    const key = try registry.accountFileKey(allocator, account_key);
    defer allocator.free(key);
    const name = try std.fmt.allocPrint(allocator, "{s}.refresh-state.json", .{key});
    defer allocator.free(name);
    return std.fs.path.join(allocator, &.{ codex_home, "accounts", name });
}

fn refreshHash(allocator: std.mem.Allocator, data: []const u8) ![64]u8 {
    var parsed = try std.json.parseFromSlice(std.json.Value, allocator, data, .{});
    defer parsed.deinit();
    if (parsed.value != .object) return error.MissingTokens;
    const tokens = parsed.value.object.get("tokens") orelse return error.MissingTokens;
    if (tokens != .object) return error.MissingTokens;
    const value = tokens.object.get("refresh_token") orelse return error.MissingRefreshToken;
    if (value != .string or std.mem.trim(u8, value.string, " \t\r\n").len == 0) return error.MissingRefreshToken;
    var digest: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(value.string, &digest, .{});
    return std.fmt.bytesToHex(digest, .lower);
}

fn saveState(allocator: std.mem.Allocator, path: []const u8, state: RefreshState) !void {
    var out: std.Io.Writer.Allocating = .init(allocator);
    defer out.deinit();
    try std.json.Stringify.value(state, .{}, &out.writer);
    sensitive_file.writeAtomic(path, out.written()) catch return error.RefreshPersistence;
}

fn clearState(path: []const u8) !void {
    std.Io.Dir.cwd().deleteFile(app_runtime.io(), path) catch |err| switch (err) {
        error.FileNotFound => {},
        else => return error.RefreshPersistence,
    };
}

fn recoverState(allocator: std.mem.Allocator, codex_home: []const u8, account_key: []const u8, snapshot_path: []const u8, state_path: []const u8) !bool {
    const data = readFile(allocator, state_path) catch |err| switch (err) {
        error.FileNotFound => return false,
        else => return error.RefreshPersistence,
    };
    defer allocator.free(data);
    var parsed = std.json.parseFromSlice(RefreshState, allocator, data, .{}) catch return error.RefreshPersistence;
    defer parsed.deinit();
    if (parsed.value.source_refresh_hash.len != 64) return error.RefreshPersistence;
    for (parsed.value.source_refresh_hash) |byte| {
        if (!std.ascii.isHex(byte)) return error.RefreshPersistence;
    }
    const current = try readFile(allocator, snapshot_path);
    defer allocator.free(current);
    const current_hash = try refreshHash(allocator, current);
    const state = parsed.value;
    const updated = state.auth orelse {
        if (std.mem.eql(u8, &current_hash, state.source_refresh_hash)) return error.RefreshUncertain;
        try clearState(state_path);
        return false;
    };
    const updated_hash = try refreshHash(allocator, updated);
    if (!std.mem.eql(u8, &current_hash, state.source_refresh_hash) and !std.mem.eql(u8, &current_hash, &updated_hash)) {
        try clearState(state_path);
        return false;
    }
    if (try activeAuthMatchesAccount(allocator, codex_home, account_key)) {
        const active_path = try registry.activeAuthPath(allocator, codex_home);
        defer allocator.free(active_path);
        const native = try readFile(allocator, active_path);
        defer allocator.free(native);
        const native_hash = try refreshHash(allocator, native);
        if (!std.mem.eql(u8, &native_hash, state.source_refresh_hash) and !std.mem.eql(u8, &native_hash, &updated_hash)) {
            try clearState(state_path);
            return false;
        }
        sensitive_file.writeAtomic(active_path, updated) catch return error.RefreshPersistence;
    }
    sensitive_file.writeAtomic(snapshot_path, updated) catch return error.RefreshPersistence;
    try clearState(state_path);
    return true;
}

fn lockExclusive(file: std.Io.File) !void {
    try file.lock(app_runtime.io(), .exclusive);
}

fn activeAuthMatchesAccount(allocator: std.mem.Allocator, codex_home: []const u8, account_key: []const u8) !bool {
    const active_path = try registry.activeAuthPath(allocator, codex_home);
    defer allocator.free(active_path);
    const active_data = readFile(allocator, active_path) catch |err| switch (err) {
        error.FileNotFound => return false,
        else => return err,
    };
    defer allocator.free(active_data);
    const info = auth.parseAuthInfoData(allocator, active_data) catch return false;
    defer info.deinit(allocator);
    const record_key = info.record_key orelse return false;
    return std.mem.eql(u8, record_key, account_key);
}

fn requestAndApply(allocator: std.mem.Allocator, data: []const u8, now: i64, requester: anytype) ![]u8 {
    var parsed = try std.json.parseFromSlice(std.json.Value, allocator, data, .{});
    defer parsed.deinit();
    const obj = switch (parsed.value) {
        .object => |value| value,
        else => return error.InvalidAuthJson,
    };
    const tokens = switch (obj.get("tokens") orelse return error.MissingTokens) {
        .object => |value| value,
        else => return error.MissingTokens,
    };
    const refresh_token = switch (tokens.get("refresh_token") orelse return error.MissingRefreshToken) {
        .string => |value| value,
        else => return error.MissingRefreshToken,
    };

    var request_writer: std.Io.Writer.Allocating = .init(allocator);
    defer request_writer.deinit();
    try std.json.Stringify.value(.{ .client_id = client_id, .grant_type = "refresh_token", .refresh_token = refresh_token }, .{}, &request_writer.writer);
    const response = requester(allocator, token_endpoint, request_writer.written()) catch |err| switch (err) {
        error.TimedOut, error.RequestFailed => return error.RefreshUncertain,
        else => return err,
    };
    defer allocator.free(response.body);
    if (response.status_code == null or response.status_code.? < 200 or response.status_code.? >= 300) {
        return switch (classifyFailure(response.status_code, response.body)) {
            .permanent => error.RefreshLoginRequired,
            .protocol => error.RefreshProtocolFailure,
            .transient => error.RefreshTransient,
        };
    }
    return try applyRefreshResponse(allocator, data, response.body, now);
}

pub fn applyRefreshResponse(allocator: std.mem.Allocator, data: []const u8, response_body: []const u8, now: i64) ![]u8 {
    var parsed = try std.json.parseFromSlice(std.json.Value, allocator, data, .{});
    defer parsed.deinit();
    if (parsed.value != .object) return error.InvalidAuthJson;
    const json_allocator = parsed.arena.allocator();
    const obj = &parsed.value.object;
    const tokens_value = obj.getPtr("tokens") orelse return error.MissingTokens;
    if (tokens_value.* != .object) return error.MissingTokens;
    const tokens = &tokens_value.object;
    var response_json = try std.json.parseFromSlice(std.json.Value, allocator, response_body, .{});
    defer response_json.deinit();
    const response_obj = switch (response_json.value) {
        .object => |value| value,
        else => return error.InvalidRefreshResponse,
    };
    const access = response_obj.get("access_token") orelse return error.InvalidRefreshResponse;
    if (access != .string or std.mem.trim(u8, access.string, " \t\r\n").len == 0) return error.InvalidRefreshResponse;
    inline for (.{ "id_token", "access_token", "refresh_token" }) |field| {
        if (response_obj.get(field)) |value| switch (value) {
            .string => |text| {
                if (std.mem.trim(u8, text, " \t\r\n").len == 0) return error.InvalidRefreshResponse;
                try tokens.put(json_allocator, field, value);
            },
            else => return error.InvalidRefreshResponse,
        };
    }
    const timestamp = try formatTimestamp(allocator, now);
    defer allocator.free(timestamp);
    try obj.put(json_allocator, "last_refresh", .{ .string = timestamp });
    var out: std.Io.Writer.Allocating = .init(allocator);
    errdefer out.deinit();
    try std.json.Stringify.value(parsed.value, .{ .whitespace = .indent_2 }, &out.writer);
    try out.writer.writeAll("\n");
    return try out.toOwnedSlice();
}

fn readFile(allocator: std.mem.Allocator, path: []const u8) ![]u8 {
    const file = try std.Io.Dir.cwd().openFile(app_runtime.io(), path, .{});
    defer file.close(app_runtime.io());
    var buffer: [4096]u8 = undefined;
    var reader = file.reader(app_runtime.io(), &buffer);
    return try reader.interface.allocRemaining(allocator, .limited(10 * 1024 * 1024));
}

fn formatTimestamp(allocator: std.mem.Allocator, seconds: i64) ![]u8 {
    if (seconds < 0) return error.InvalidTimestamp;
    const epoch = std.time.epoch.EpochSeconds{ .secs = @intCast(seconds) };
    const year_day = epoch.getEpochDay().calculateYearDay();
    const month_day = year_day.calculateMonthDay();
    const day_seconds = epoch.getDaySeconds();
    return std.fmt.allocPrint(allocator, "{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}Z", .{
        year_day.year,                 month_day.month.numeric(),        month_day.day_index + 1,
        day_seconds.getHoursIntoDay(), day_seconds.getMinutesIntoHour(), day_seconds.getSecondsIntoMinute(),
    });
}
