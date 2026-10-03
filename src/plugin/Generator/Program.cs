using System.Text.Json;
using Mutagen.Bethesda;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Skyrim;
using YamlDotNet.Core;

namespace CardboardFaceMask.Generator;

/// <summary>
/// Command line:
///   generate     --config config.yaml --manifest work/nif_manifest.json --out work/plugin/generated/CardboardFaceMask.esp
///                [--vanilla-json src/plugin/vanilla_formkeys.json]
///   canonicalize &lt;in.esp&gt; &lt;out.esp&gt;   (rewrite with records in FormID order; in == out is allowed)
///   dump         &lt;plugin.esp&gt;
///   compare      &lt;a.esp&gt; &lt;b.esp&gt;      (record-level equality through Mutagen)
/// </summary>
public static class Program
{
    public static int Main(string[] args)
    {
        try
        {
            return args.FirstOrDefault() switch
            {
                "generate" => Generate(ParseOptions(args.Skip(1))),
                "canonicalize" when args.Length == 3 => Canonicalize(args[1], args[2]),
                "dump" when args.Length == 2 => DumpFile(args[1]),
                "compare" when args.Length == 3 => Compare(args[1], args[2]),
                _ => Usage(),
            };
        }
        catch (Exception e) when (e is InvalidDataException or IOException or ArgumentException
                                      or JsonException or YamlException or KeyNotFoundException or FormatException)
        {
            // Input problems (bad config/manifest, missing files) get a one-line message, not a stack trace.
            Console.Error.WriteLine($"ERROR: {e.Message}");
            return 1;
        }
    }

    private static int Usage()
    {
        Console.Error.WriteLine("usage: generate --config <config.yaml> --manifest <nif_manifest.json> --out <plugin.esp> [--vanilla-json <file>]");
        Console.Error.WriteLine("       canonicalize <in.esp> <out.esp>");
        Console.Error.WriteLine("       dump <plugin.esp>");
        Console.Error.WriteLine("       compare <a.esp> <b.esp>");
        return 2;
    }

    private static Dictionary<string, string> ParseOptions(IEnumerable<string> args)
    {
        var opts = new Dictionary<string, string>();
        var list = args.ToList();
        if (list.Count % 2 != 0) throw new ArgumentException("options must come in '--name value' pairs");
        for (int i = 0; i + 1 < list.Count; i += 2)
        {
            if (!list[i].StartsWith("--")) throw new ArgumentException($"unexpected argument '{list[i]}'");
            opts[list[i][2..]] = list[i + 1];
        }
        foreach (var key in new[] { "config", "manifest", "out" })
            if (!opts.ContainsKey(key)) throw new ArgumentException($"missing --{key}");
        return opts;
    }

    private static int Generate(Dictionary<string, string> opts)
    {
        var inputs = BuildInputs.Load(opts["config"], opts["manifest"]);
        if (!inputs.ManifestFound)
            Console.WriteLine($"NOTE: {opts["manifest"]} not found; using contract default model paths.");

        string outPath = Path.GetFullPath(opts["out"]);
        if (!string.Equals(Path.GetFileName(outPath), inputs.PluginName, StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException($"--out file name must be the plugin name '{inputs.PluginName}'");
        Directory.CreateDirectory(Path.GetDirectoryName(outPath)!);

        // Same writer as `canonicalize`, so the generator output and the canonicalized Spriggit
        // rebuild can be compared byte for byte (build_plugin.py gate (a)).
        var mod = PluginBuilder.Build(inputs);
        File.WriteAllBytes(outPath, CanonicalBytes(mod));

        using var written = Read(outPath);
        Checks.Validate(written, inputs);
        if (opts.TryGetValue("vanilla-json", out var vanillaJson))
            File.WriteAllText(vanillaJson, Vanilla.ToJson());
        Console.WriteLine($"Wrote {outPath}");
        Console.Write(Dump.Render(written));
        Console.WriteLine("Contract checks: OK");
        return 0;
    }

    /// <summary>
    /// Rewrites a plugin with the records of every GRUP in FormID order. Spriggit rebuilds a GRUP in
    /// directory-listing order (filesystem dependent), so this makes the shipped bytes reproducible.
    /// The field dump before and after must match: only the order may change.
    /// </summary>
    private static int Canonicalize(string input, string output)
    {
        byte[] bytes;
        string before;
        using (var mod = Read(input))
        {
            before = Dump.Render(mod);
            bytes = CanonicalBytes(mod);
        }
        File.WriteAllBytes(output, bytes);
        using var written = Read(output);
        if (Dump.Render(written) != before)
            throw new InvalidDataException($"canonicalize changed record content of {input}");
        Console.WriteLine($"Canonicalized {input} -> {output}");
        return 0;
    }

    private static int DumpFile(string path)
    {
        using var mod = Read(path);
        Console.Write(Dump.Render(mod));
        return 0;
    }

    /// <summary>
    /// Record-level equality: both plugins are canonicalized (see CanonicalBytes) and compared byte
    /// for byte, and their field dumps must match. (Mutagen's ArmorAddon.Equals is not usable here:
    /// it returns false even for two reads of the same file, because GenderedItem has no value equality.)
    /// </summary>
    private static int Compare(string a, string b)
    {
        using var modA = Read(a);
        using var modB = Read(b);
        bool sameDump = Dump.Render(modA) == Dump.Render(modB);
        bool sameCanonical = CanonicalBytes(modA).AsSpan().SequenceEqual(CanonicalBytes(modB));
        Console.WriteLine($"field dump equal: {sameDump}; canonical Mutagen re-write equal: {sameCanonical}");
        bool equal = sameDump && sameCanonical;
        Console.WriteLine(equal ? "record-level equal" : "records DIFFER");
        return equal ? 0 : 1;
    }

    /// <summary>
    /// The canonical bytes of a plugin: header and records deep-copied into a fresh mod, records
    /// added in FormID order, written by Mutagen (to a temp file named after the ModKey).
    /// </summary>
    private static byte[] CanonicalBytes(ISkyrimModGetter source)
    {
        int known = source.Armors.Count + source.ArmorAddons.Count;
        if (source.EnumerateMajorRecords().Count() != known)
            throw new InvalidDataException($"{source.ModKey}: unexpected record types; only ARMO/ARMA are handled");
        var copy = new SkyrimMod(source.ModKey, SkyrimRelease.SkyrimSE, source.ModHeader.Stats.Version);
        copy.ModHeader.DeepCopyIn(source.ModHeader);
        foreach (var r in source.Armors.OrderBy(r => r.FormKey.ID))
            copy.Armors.Add(r.DeepCopy());
        foreach (var r in source.ArmorAddons.OrderBy(r => r.FormKey.ID))
            copy.ArmorAddons.Add(r.DeepCopy());
        string dir = Directory.CreateTempSubdirectory("cfm-canonical-").FullName;
        try
        {
            string path = Path.Combine(dir, source.ModKey.FileName);
            copy.BeginWrite.ToPath(path).WithNoLoadOrder().Write();
            return File.ReadAllBytes(path);
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }

    private static ISkyrimModDisposableGetter Read(string path) =>
        SkyrimMod.CreateFromBinaryOverlay(new ModPath(path), SkyrimRelease.SkyrimSE);
}
