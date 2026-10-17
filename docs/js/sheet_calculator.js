/* Exact JS port of sheet_calculator.py (the stadium sheet's calculator tab, Q25:V75 + Y:AB assembly).
 * Keep this file and sheet_calculator.py in lockstep; tools/verify_sheet_calculator.py checks the Python
 * against the sheet and tools/verify_calculator_js.py checks this file against the Python.
 * Blank cells are null (0 in arithmetic, equal to both 0 and ""). */
(function (root) {
  "use strict";
  var COLS = "DEFGHIJKLMN";
  var SNAP = true;

  function n(x) { return x == null || x === "" ? 0 : x; }
  function blank(x) { return x == null || x === ""; }
  function rd(x) {                       // ROUNDDOWN(x, 0)
    if (SNAP && Math.abs(x - Math.round(x)) < 1e-9) return Math.round(x);
    return Math.trunc(x);
  }
  function rnd(x) { return (x >= 0 ? 1 : -1) * Math.floor(Math.abs(x) + 0.5 + 1e-12); }   // ROUND, half away
  function clamp5(v) { return v > 5 ? 5 : (v < -5 ? -5 : v); }
  function sum(a) { var s = 0; for (var i = 0; i < a.length; i++) s += a[i]; return s; }

  function Calc(params) { this.p = params; }
  Calc.prototype.c = function (k) { var v = this.p[k]; return v === undefined ? null : v; };
  Calc.prototype.cn = function (k) { return n(this.c(k)); };

  Calc.prototype.avgRate = function (rows, bat, pit) {
    var lab = { CON: "MOV", EYE: "CMD", POW: "VEL", SPD: "AWR" }, vals = [];
    for (var i = 0; i < rows.length; i++) {
      var stat = this.c("A" + rows[i]);
      var diff = Math.trunc(bat[stat] - pit[lab[stat]]);
      vals.push(this.cn(COLS[diff + 5] + rows[i]));
    }
    return sum(vals) / vals.length;
  };

  Calc.prototype.tables = function (dynamic, pit, bat) {
    var cn = this.cn.bind(this), names = [], off = {}, center = {}, tbl = {}, r, i, nm;
    for (r = 11; r <= 19; r++) names.push(this.c("A" + r));
    for (var k = 0; k < names.length; k++) {
      r = 11 + k; nm = names[k];
      var right = ["J", "K", "L", "M", "N"].map(function (col) { return cn(col + r); });
      var left = right.slice().reverse().map(function (v) { return -v; });
      off[nm] = left.concat([0], right);
      center[nm] = cn("I" + r);
    }
    names.forEach(function (name) {
      var vals = new Array(11).fill(0); vals[5] = center[name];
      for (var j = 6; j < 11; j++) vals[j] = vals[j - 1] + off[name][j];
      for (j = 4; j >= 0; j--) { var s = vals[j + 1] + off[name][j]; vals[j] = s < 1 ? 1 : s; }
      tbl[name] = vals;
    });
    var po_h = tbl["PO"], fo = new Array(11).fill(0), po = new Array(11).fill(0);
    if (dynamic) {
      var n97 = this.avgRate([99, 100, 101, 102], bat, pit), m104 = this.avgRate([106, 107, 108, 109], bat, pit);
      var n104 = (1 - n97) * m104;
      fo.fill(n104); po.fill(1 - m104);
    } else {
      fo[5] = cn("I21"); po[5] = cn("I22");
      for (i = 4; i >= 0; i--) {
        fo[i] = (off["FO"][i] / 1000) * 2 + fo[i + 1];
        po[i] = ((po_h[i] - po_h[i + 1]) / 1000) * 2 + po[i + 1];
      }
      for (i = 6; i < 11; i++) {
        fo[i] = (off["FO"][i] / 1000) * 2 + fo[i - 1];
        po[i] = ((po_h[i] - po_h[i - 1]) / 1000) * 2 + po[i - 1];
      }
    }
    return { tbl: tbl, fo: fo, po: po };
  };

  Calc.prototype.goRates = function (j61, z1, z2, z3, q5, ac17, aa6) {
    var cn = this.cn.bind(this);
    var c54 = cn("C54"), c55 = cn("C55"), d55 = cn("D55"), f55 = cn("F55"), g55 = cn("G55");
    var c56 = cn("C56"), d56 = cn("D56"), f56 = cn("F56"), g56 = cn("G56"), e57 = cn("E57"), g58 = cn("G58");
    var zs = [z1, z2, z3].filter(function (v) { return !blank(v); });
    var avgAll = zs.length ? sum(zs) / zs.length : null;
    var z12 = [z1, z2].filter(function (v) { return !blank(v); });
    var avg12 = z12.length ? sum(z12) / z12.length : null;
    var out = {}, k = j61;
    if (k === 0) { out.GO = 1; }
    else if (k === 1) {
      var c62 = c55 + ((z1 - 3) * c56), c67 = c54 - ((aa6 - 3) * 0.1);
      out.GORA = c62; out.FC = 1 - c67 - c62; out.DP = c67;
    } else if (k === 2) {
      var d62 = d55 + ((z2 - 3) * d56); out.GORA = d62; out.GO = 1 - d62;
    } else if (k === 3) {
      var e62 = d55 + ((z3 - 3) * d56); out.GORA = e62; out.GO = 1 - e62;
    } else if (k === 4) {
      var f62 = c55 + ((avg12 - 3) * c56);
      var f70 = ((c54 - ((aa6 - 3) * 0.1)) / 2) * (1 + e57), f69 = f70;
      var f64 = (1 - f69 - f70 - f62) / 2;
      out.GORA = f62; out.FC = f64; out.FC3rd = f64; out.DP21 = f69; out.DP31 = f70;
    } else if (k === 5) {
      var g62 = c55 + ((avgAll - 3) * c56);
      var g67 = ((c54 - ((aa6 - 3) * 0.1)) / 2) * (1 + e57);
      var g68 = q5 > 0 ? null : (1 - (c55 + ((avgAll - 3) * c56)) - g67 - g67) / 2;
      var g64 = 1 - g67 - g62 - n(g68);
      out.GORA = g62; out.FC = g64; out.DP = g67;
      if (g68 !== null) out.DPRun = g68;
    } else if (k === 6) {
      var h62 = f55 + ((avgAll - 3) * f56); out.GORA = h62; out.GO = 1 - h62;
    } else {
      var i62 = g55, i66 = g58 + ((avgAll - 3) * g56);
      var i70 = rd((1 - i66 - i62) / (ac17 ? 4 : 3) * 1000) / 1000;
      var i71 = rd((1 - i66 - i62) / (ac17 ? 2 : 3) * 1000) / 1000;
      out.GORA = i62; out.FCH = i66; out.DP21 = 1 - i62 - i70 - i71 - i66; out.DP31 = i70; out.DPH1 = i71;
    }
    return out;
  };

  function assemble(rows) {
    var rs = rows.filter(function (r) { return r[1] > 0; }).map(function (r) { return [r[0], Math.trunc(r[1]), r[2]]; });
    rs.sort(function (a, b) { return a[2] - b[2]; });
    var out = [], lo = 0;
    rs.forEach(function (r) { out.push([r[0], r[1], lo, lo + r[1] - 1]); lo += r[1]; });
    return out;
  }

  /* o: {pit_hand,pit_mov,pit_cmd,pit_vel,pit_awr,bat_hand,bat_con,bat_eye,bat_pow,bat_spd,
         runner_speeds:[s1,s2,s3] (null = empty), outs, hit_and_run, infield_in, dynamic, include_lotp,
         catcher_eye (optional, steal safe ranges)} */
  Calc.prototype.compute = function (o) {
    var self = this, cn = this.cn.bind(this), c = this.c.bind(this);
    var q5 = Math.trunc(o.outs), q7 = !!o.hit_and_run, ac9 = q7 ? 1 : 0;
    var dyn = o.dynamic == null ? !!c("D9") : !!o.dynamic;
    var lotp = o.include_lotp == null ? !!c("U22") : !!o.include_lotp;
    var bat = { CON: o.bat_con - (q7 ? 1 : 0), EYE: o.bat_eye, POW: o.bat_pow, SPD: o.bat_spd };
    var pit = { MOV: o.pit_mov, CMD: o.pit_cmd, VEL: o.pit_vel, AWR: o.pit_awr };
    var z = o.runner_speeds.map(function (s) { return blank(s) ? null : s + ac9; });
    var z1 = z[0], z2 = z[1], z3 = z[2];
    var zn = z.map(n), sumZ = sum(zn);
    var ac17 = q5 === 2 ? false : !!(o.infield_in && !blank(z3) && z3 !== 0);
    var code = (!blank(z3) ? 100 : 0) + (!blank(z2) ? 10 : 0) + (!blank(z1) ? 1 : 0);
    var j61 = { 0: 0, 1: 1, 10: 2, 11: 4, 100: 3, 101: 5, 110: 6, 111: 7 }[code];
    var n9 = cn("N9");
    var bh = String(o.bat_hand).toUpperCase(), ph = String(o.pit_hand).toUpperCase();
    var w7 = bh === "S" ? 1 : (ph === bh ? 1 - n9 : 1 + n9);
    var aa6 = o.bat_spd, aa3 = o.pit_awr, x3 = o.pit_mov;
    var T3 = this.tables(dyn, pit, bat), tbl = T3.tbl, foPct = T3.fo, poPct = T3.po;

    function sumif(label) {
      var s = 0;
      for (var r = 40; r <= 50; r++) if (c("A" + r) === label) s += cn("I" + r);
      return s;
    }
    var statOf = {};
    for (var r0 = 11; r0 <= 20; r0++) statOf[c("A" + r0)] = [c("B" + r0), c("C" + r0)];
    function matchup(label) { var s = statOf[label]; return Math.trunc(clamp5(bat[s[0]] - pit[s[1]])); }
    var vHR = matchup("HR"), v3B = matchup("3B"), v2B = matchup("2B"), v1B = matchup("Hit"),
      vIF = matchup("IF1B"), vBB = matchup("BB"), vFO = matchup("FO"), vPO = matchup("PO"), vK = matchup("K");
    var mx = function (x) { return Math.max(1, x); };

    var w11 = mx(rd((tbl["HR"][vHR + 5] + sumif("HR")) * w7));
    var w12 = mx(rd((tbl["3B"][v3B + 5] + sumif("3B")) * w7));
    var w13 = mx(rd((tbl["2B"][v2B + 5] + sumif("2B")) * w7));
    var r40 = cn(COLS[v2B + 5] + "40");
    var awrIdx = Math.trunc(o.pit_awr - 3);
    if (awrIdx < -5 || awrIdx > 5) throw new Error("pitcher AWR out of range");
    var r41 = cn(COLS[awrIdx + 5] + "41");
    var w14 = mx(rd(tbl["Hit"][v1B + 5] * w7) + cn(COLS[v1B + 5] + "43") + r40 + r41 + cn("I42")
      - (w11 + w12 + w13) + (ac17 ? 20 : 0));
    var w15 = mx(rd((tbl["IF1B"][vIF + 5] + sumif("IF1B")) * w7));
    var w16 = mx(rd((tbl["BB"][vBB + 5] + sumif("BB")) * w7));
    var foV = foPct[vFO + 5], poV = poPct[vPO + 5];
    var s16 = w11 + w12 + w13 + w14 + w15 + w16;
    var w17 = mx(rd((500 - s16) * foV - (500 - s16) * foV * poV));
    var w18 = mx(rd((500 - w16) * foV * poV));
    var w19 = mx(rd((tbl["K"][vK + 5] + sumif("K")) * w7));
    var w21 = sumZ === 0 ? 0 : (q5 === 2 ? 0 : cn("V21"));
    var w20 = 500 - (w11 + w12 + w13 + w14 + w15 + w16 + w17 + w18 + w19) - w21 + 1;
    var base = { HR: w11, "3B": w12, "2B": w13, "1B": w14, IF1B: w15, BB: w16, FO: w17, PO: w18, K: w19, GO: w20, LO: w21 };

    var c74 = cn("C74"), c75 = cn("C75"), c76 = cn("C76"), c77 = cn("C77");
    var g75 = q5 === 2 ? c76 : (q7 ? c77 : 1);
    var f75 = blank(z2) ? z1 : z2;
    var f76 = blank(z1) ? null : (z1 > n(z2) ? z2 : z1);
    var g76 = blank(f76) ? null : g75;
    var wh = function (f, g) { var d = f - 3; return (c74 + (d * (d < 0 ? 0.5 : 1) * c75)) * g; };
    var h75, h76, i75;
    if (blank(f75)) h75 = null; else if (q7) h75 = f75 > 0 ? 1 : null; else h75 = wh(f75, g75);
    if (blank(z1)) h76 = null; else if (q7) h76 = n(f76) > 0 ? 1 : null; else if (blank(f76)) h76 = null; else h76 = wh(f76, g76);
    if (blank(z2)) i75 = 0; else i75 = q5 === 2 ? 0 : c74 + (((f75 - 3) * ((f75 - 3) < 0 ? 0.5 : 1)) * c75);

    var T = {}, V = {}, U = {}, r;
    U[26] = base.HR; T[26] = 1; V[26] = rd(T[26] * U[26]) === 0 ? 1 : U[26]; V[27] = null;
    U[28] = base["3B"]; T[28] = 1; V[28] = rd(T[28] * U[28]); V[29] = null;
    U[30] = U[31] = U[32] = base["2B"];
    T[30] = n(z1) === 0 ? 0 : (blank(h76) ? n(h75) : h76);
    T[31] = 1 - T[30];
    V[30] = rd(U[30] * T[30]); V[31] = U[31] - V[30]; V[32] = null;
    U[33] = U[34] = U[35] = U[36] = base["1B"];
    if (n(z1) + n(z2) === 0) T[33] = 0; else T[33] = blank(h76) ? (blank(h75) ? 0 : h75) : h76;
    if (blank(z2) || n(z2) === 0 || blank(z1) || n(z1) === 0) T[34] = 0;
    else T[34] = n(h75) === n(h76) ? 0 : n(h75) - n(h76);
    T[35] = 1 - T[33] - T[34];
    var U37 = base.IF1B;
    V[33] = rd(T[33] * U[33]) + ((q7 && ac17) ? U37 : 0);
    V[34] = rd(T[34] * U[34]);
    V[35] = (U[35] - V[33] - V[34]) + (ac17 ? U37 : 0); V[36] = null;
    U[37] = U37; T[37] = ac17 ? 0 : 1; V[37] = rd(T[37] * U[37]); V[38] = null;
    U[39] = base.BB; T[39] = 1; V[39] = rd(T[39] * U[39]); V[40] = null;
    U[41] = U[42] = U[43] = U[44] = U[45] = base.FO;
    T[41] = (q5 === 2 || n(z3) === 0 || n(z2) === 0) ? 0 : i75;
    T[42] = (q5 === 2 || n(z2) === 0) ? 0 : (T[41] > 0 ? 0 : i75);
    if (n(z2) + n(z3) === 0) T[43] = 0; else if (q5 === 2 || n(z3) === 0) T[43] = 0; else T[43] = 1 - T[41];
    if (q5 === 2) T[44] = 1; else if (T[43] > 0) T[44] = 0; else if (sumZ === 0) T[44] = 1; else T[44] = 1 - T[42] - T[41];
    V[41] = rd(T[41] * U[41]); V[42] = rd(T[42] * U[42]);
    V[43] = T[43] === 0 ? 0 : U[43] - V[41] - V[42];
    V[44] = T[43] > 0 ? 0 : U[44] - V[42]; V[45] = null;
    U[46] = U[47] = base.PO; T[46] = 1; V[46] = rd(T[46] * U[46]); V[47] = null;

    var go = this.goRates(j61, z1, z2, z3, q5, ac17, aa6);
    var order = [[48, "GORA"], [49, "FCH"], [50, "FC"], [51, "GO"], [52, "FC3rd"], [53, "DPRun"], [54, "DP"],
      [55, "DP21"], [56, "DP31"], [57, "DPH1"]];
    for (r = 48; r <= 58; r++) U[r] = base.GO;
    order.forEach(function (e) {
      var rr = e[0], raw = go[e[1]] === undefined ? null : go[e[1]];
      if (rr === 48) T[rr] = ac17 ? 0 : (q5 === 2 ? null : raw);
      else if (rr === 51) T[rr] = raw;
      else T[rr] = q5 === 2 ? null : raw;
    });
    var tsum = function (a, b) { var s = 0; for (var i = a; i <= b; i++) s += n(T[i]); return s; };
    V[48] = rd(n(T[48]) * U[48]);
    for (r = 49; r <= 57; r++) {
      var t = T[r], prev = 0;
      for (var i = 48; i < r; i++) prev += n(V[i]);
      if ((r === 49 || r === 50) && blank(t)) V[r] = 0;
      else if (tsum(r + 1, 58) === 0) V[r] = U[r] - prev;
      else V[r] = rd(n(t) * U[r]);
    }
    V[58] = null;
    U[59] = base.K; T[59] = 1;
    U[61] = U[62] = U[63] = base.LO;
    T[62] = q5 > 0 ? 0 : (n(z2) === 0 ? 0 : (n(z1) === 0 ? 0 : 1));
    if (!lotp) T[63] = 0;
    else if (T[62] > 0 || q5 > 0) T[63] = 0;
    else T[63] = z.filter(function (v) { return n(v) > 0; }).length < 2 ? 0 : 0.25;
    if (T[62] > 0) T[61] = 0;
    else if (q5 > 1) T[61] = 0;
    else T[61] = (sumZ === 0 ? 0 : (n(z1) > 0 ? 0 : 1)) - T[63];
    V[62] = T[62] * U[62];
    var s3 = T[61] + T[62] + T[63];
    V[63] = T[63] === 0 ? 0 : (s3 === 0 ? 0 : 1);
    V[61] = s3 === 0 ? 0 : U[61] - (V[62] + V[63]);
    V[59] = rd(T[59] * U[59]) + (s3 === 0 ? base.LO : 0); V[60] = null;

    var c80 = cn("C80"), c81 = cn("C81"), c82 = cn("C82");
    var m80 = -1; for (r = 26; r <= 38; r++) m80 += n(V[r]);
    var h80 = aa6 - x3, m81 = rnd((1 + (c80 + h80 * c81)) * m80) - c82, h81 = aa6 - aa3;
    var f82 = n(z3) > 0 ? "Runner Home" : n(z2) > 0 ? "Runner to 3rd" : n(z1) > 0 ? "Runner to 2nd" : "Bunt for Hit";
    var hcol = COLS[Math.min(5, Math.trunc(h81)) + 5];
    var rowOf = {}; for (r = 86; r <= 89; r++) rowOf[c("A" + r)] = r;
    var t71, t72;
    if (j61 === 0) { t71 = 0; t72 = 0; } else { t71 = cn(hcol + rowOf[f82]); t72 = cn(hcol + "89"); }
    var t73 = 1 - t71 - t72;
    var u67 = c82, u68 = m81, u69 = base.BB, u70 = base.K, u74 = U[62], u75 = U[63];
    var u71 = 500 - (u67 + u68 + u69 + u70) - (u74 + u75);
    var v71 = rd(t71 * u71), v72 = rd(t72 * u71), v74 = T[62] * u74, v75 = T[63] * u75;
    var v73 = (500 - (u67 + u68 + u69 + u70 + v71 + v72) + 1) - (v74 + v75);
    var goName = j61 === 0 ? "GO" : "BFC";
    var bunt = [
      ["B1BWH", "B1BWH", 1, 1, u67, u67], ["B1B", "B1B", 2, 1, u68, u68], ["BB", "BB", 3, 1, u69, u69],
      ["K", "K", 5, 1, u70, u70], ["SacB", "SacB", 4, t71, u71, v71], ["BDP", "BDP", 7, t72, u71, v72],
      [goName, goName, 6, t73, u71, v73], ["TP", "TP", 8, T[62], u74, v74], ["LOTP", "LOTP", 9, T[63], u75, v75]
    ];
    var labels = {
      26: ["HR", "HR", 1], 28: ["3B", "3B", 2], 30: ["2B", "2BWH", 3], 31: ["2B", "2B", 4],
      33: ["1B", "1BWH", 5], 34: ["1B", "1BWH2", 6], 35: ["1B", "1B", 7], 37: ["IF1B", "IF1B", 8],
      39: ["BB", "BB", 9], 41: ["FO", "DSacF", 11], 42: ["FO", "DFO", 12], 43: ["FO", "SacF", 13],
      44: ["FO", "FO", 14], 46: ["PO", "PO", 15], 48: ["GO", "GORA", 10], 49: ["GO", "FCH", 16],
      50: ["GO", "FC", 17], 51: ["GO", "GO", 18], 52: ["GO", "FC3rd", 19], 53: ["GO", "DPRun", 20],
      54: ["GO", "DP", 22], 55: ["GO", "DP21", 23], 56: ["GO", "DP31", 24], 57: ["GO", "DPH1", 25],
      59: ["K", "K", 21], 61: ["LO", "LODP", 30], 62: ["LO", "TP", 31], 63: ["LO", "LOTP", 32]
    };
    var swing = [];
    Object.keys(labels).forEach(function (k) {
      var l = labels[k], rr = +k;
      swing.push({ row: rr, result: l[0], sub: l[1], order: l[2], rate: T[rr] === undefined ? null : T[rr],
        base: U[rr] === undefined ? null : U[rr], final: n(V[rr]) });
    });
    var buntRows = bunt.map(function (b, i) {
      return { row: 67 + i, result: b[0], sub: b[1], order: b[2], rate: b[3], base: b[4], final: b[5] };
    });

    // steals: safe range per runner (AA11:AB13) when a catcher EYE is supplied
    var steals = null;
    if (o.catcher_eye != null) {
      steals = [];
      for (var i2 = 0; i2 < 3; i2++) {
        if (blank(z[i2])) { steals.push(null); continue; }
        // steals use the runner's unadjusted speed (the hit-and-run +1 applies to the swing only)
        var aa = o.runner_speeds[i2] - ((aa3 + o.catcher_eye) / 2);
        var hdr = null;
        for (var col = 2; col <= 22; col++) {
          var cc = String.fromCharCode(65 + col);       // C..W
          if (Math.abs(cn(cc + "92") - aa) < 1e-9) { hdr = cc; break; }
        }
        steals.push({ base: ["2nd", "3rd", "Home"][i2], matchup: aa, safe: hdr ? cn(hdr + (93 + i2)) : null });
      }
    }
    return {
      swing: swing, bunt: buntRows, base_ranges: base, steals: steals,
      swing_ranges: assemble(swing.map(function (s) { return [s.sub, s.final, s.order]; })),
      bunt_ranges: assemble(buntRows.map(function (b) { return [b.sub, b.final, b.order]; }))
    };
  };

  root.SheetCalculator = Calc;
  root.SheetCalculator.setSnap = function (v) { SNAP = !!v; };
})(typeof window !== "undefined" ? window : this);
