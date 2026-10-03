using System.Text.Json;
using System.Text.RegularExpressions;
using YamlDotNet.Core;
using YamlDotNet.RepresentationModel;

namespace CardboardFaceMask.Generator;

/// <summary>Model paths (relative to Data\meshes, backslashes) the plugin points at.</summary>
public sealed record ModelPaths(string HumanMale, string HumanFemale, string BeastMale, string BeastFemale, string? Ground);

/// <summary>Everything the generator needs from config.yaml and work/nif_manifest.json.</summary>
public sealed record BuildInputs(string PluginName, int Slot, bool NonPlayable, ModelPaths Models, bool ManifestFound)
{
    /// <summary>Defaults from docs/CONTRACTS.md, used when the NIF stage has not produced a manifest yet.</summary>
    public static readonly ModelPaths ContractDefaults = new(
        @"CardboardFaceMask\mask_human_m.nif",
        @"CardboardFaceMask\mask_human_f.nif",
        @"CardboardFaceMask\mask_beast_m.nif",
        @"CardboardFaceMask\mask_beast_f.nif",
        null);

    // A folder or file-name stem: letters, digits, underscore (same rule as tests/test_plugin.py).
    private static readonly Regex SafeName = new("^[A-Za-z0-9_]+$", RegexOptions.CultureInvariant);

    public static BuildInputs Load(string configPath, string manifestPath)
    {
        var config = LoadYamlMapping(configPath);
        string pluginName = RequireScalar(config, "plugin_name", configPath);
        string slotText = RequireScalar(config, "slot", configPath);
        bool nonPlayable = ParseBool(RequireScalar(config, "non_playable", configPath), "non_playable");
        if (!pluginName.EndsWith(".esp", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"{configPath}: plugin_name must be an .esp (ESL-flagged ESP), got '{pluginName}'");
        if (!int.TryParse(slotText, out int slot) || slot < 30 || slot > 61)
            throw new InvalidDataException($"{configPath}: slot must be a biped slot 30..61, got '{slotText}'");

        bool found = File.Exists(manifestPath);
        var models = found ? LoadManifest(manifestPath) : ContractDefaults;
        return new BuildInputs(pluginName, slot, nonPlayable, models, found);
    }

    private static YamlMappingNode LoadYamlMapping(string path)
    {
        using var reader = new StreamReader(path);
        var stream = new YamlStream();
        try
        {
            stream.Load(reader);
        }
        catch (YamlException e)
        {
            throw new InvalidDataException($"{path}: not valid YAML: {e.Message}");
        }
        if (stream.Documents.Count == 0 || stream.Documents[0].RootNode is not YamlMappingNode map)
            throw new InvalidDataException($"{path}: top level must be a YAML mapping");
        return map;
    }

    private static string RequireScalar(YamlMappingNode map, string key, string path)
    {
        if (map.Children.TryGetValue(new YamlScalarNode(key), out var node) && node is YamlScalarNode s && s.Value != null)
            return s.Value;
        throw new InvalidDataException($"{path}: missing scalar key '{key}'");
    }

    private static bool ParseBool(string value, string key) => value.Trim().ToLowerInvariant() switch
    {
        "true" or "yes" or "on" => true,
        "false" or "no" or "off" => false,
        _ => throw new InvalidDataException($"config key '{key}' is not a boolean: '{value}'"),
    };

    /// <summary>Reads work/nif_manifest.json (schema in docs/CONTRACTS.md, stage 4).</summary>
    private static ModelPaths LoadManifest(string path)
    {
        using var doc = ParseJson(path);
        var root = doc.RootElement;
        if (root.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException($"{path}: top level must be a JSON object");

        string subdir = RequireString(root, "meshes_subdir", path, "meshes_subdir");
        if (!SafeName.IsMatch(subdir))
            throw new InvalidDataException($"{path}: meshes_subdir must be one folder name ({SafeName}), got '{subdir}'");

        if (!root.TryGetProperty("models", out var models) || models.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException($"{path}: missing object 'models'");
        string Model(string key) => CheckModelPath(RequireString(models, key, path, $"models.{key}"), subdir, path, $"models.{key}");

        string? ground = null;
        if (root.TryGetProperty("ground_model", out var g))
        {
            ground = g.ValueKind switch
            {
                JsonValueKind.Null => null,
                JsonValueKind.String => CheckModelPath(g.GetString()!, subdir, path, "ground_model"),
                _ => throw new InvalidDataException($"{path}: ground_model must be a string or null"),
            };
        }
        return new ModelPaths(Model("human_m"), Model("human_f"), Model("beast_m"), Model("beast_f"), ground);
    }

    private static JsonDocument ParseJson(string path)
    {
        try
        {
            return JsonDocument.Parse(File.ReadAllText(path));
        }
        catch (JsonException e)
        {
            throw new InvalidDataException($"{path}: not valid JSON: {e.Message}");
        }
    }

    private static string RequireString(JsonElement obj, string key, string path, string what)
    {
        if (obj.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String && !string.IsNullOrEmpty(v.GetString()))
            return v.GetString()!;
        throw new InvalidDataException($"{path}: missing or non-string '{what}'");
    }

    /// <summary>
    /// Model paths are stored exactly as the game expects: relative to meshes\, backslashes, and
    /// inside this mod's own folder: "&lt;meshes_subdir&gt;\&lt;name&gt;.nif" (no "..", no sub-folders).
    /// </summary>
    private static string CheckModelPath(string p, string subdir, string path, string what)
    {
        var parts = p.Split('\\');
        bool ok = parts.Length == 2
            && parts[0] == subdir
            && parts[1].EndsWith(".nif", StringComparison.Ordinal)
            && SafeName.IsMatch(parts[1][..^4]);
        if (!ok)
            throw new InvalidDataException(
                $"{path}: {what} must be '{subdir}\\<name>.nif' (relative to meshes\\, backslashes, name of letters/digits/_), got '{p}'");
        return p;
    }
}
