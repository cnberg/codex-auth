const std = @import("std");

pub fn parseTimestampMs(s: []const u8) ?i64 {
    if (s.len < 20) return null;
    if (s[4] != '-' or s[7] != '-' or s[10] != 'T' or s[13] != ':' or s[16] != ':') return null;

    const year = parseDecimal(s[0..4]) orelse return null;
    const month = parseDecimal(s[5..7]) orelse return null;
    const day = parseDecimal(s[8..10]) orelse return null;
    const hour = parseDecimal(s[11..13]) orelse return null;
    const minute = parseDecimal(s[14..16]) orelse return null;
    const second = parseDecimal(s[17..19]) orelse return null;

    if (month < 1 or month > 12) return null;
    if (day < 1 or day > 31) return null;
    if (hour > 23 or minute > 59 or second > 59) return null;

    var idx: usize = 19;
    var millis: i64 = 0;
    if (idx < s.len and s[idx] == '.') {
        idx += 1;
        const frac_start = idx;
        while (idx < s.len and std.ascii.isDigit(s[idx])) : (idx += 1) {}
        if (idx == frac_start) return null;

        const frac_len = idx - frac_start;
        const use_len = @min(frac_len, 3);
        millis = parseDecimal(s[frac_start .. frac_start + use_len]) orelse return null;
        if (use_len == 1) millis *= 100 else if (use_len == 2) millis *= 10;
    }

    if (idx >= s.len or s[idx] != 'Z' or idx + 1 != s.len) return null;

    const days = daysFromCivil(year, month, day);
    return (((days * 24) + hour) * 60 + minute) * 60 * 1000 + second * 1000 + millis;
}

fn parseDecimal(slice: []const u8) ?i64 {
    if (slice.len == 0) return null;
    var value: i64 = 0;
    for (slice) |ch| {
        if (!std.ascii.isDigit(ch)) return null;
        value = value * 10 + (ch - '0');
    }
    return value;
}

fn daysFromCivil(year: i64, month: i64, day: i64) i64 {
    const adjusted_year = year - (if (month <= 2) @as(i64, 1) else 0);
    const era = @divFloor(if (adjusted_year >= 0) adjusted_year else adjusted_year - 399, 400);
    const year_of_era = adjusted_year - era * 400;
    const month_prime = month + (if (month > 2) @as(i64, -3) else 9);
    const day_of_year = @divFloor(153 * month_prime + 2, 5) + day - 1;
    const day_of_era = year_of_era * 365 + @divFloor(year_of_era, 4) - @divFloor(year_of_era, 100) + day_of_year;
    return era * 146097 + day_of_era - 719468;
}
