using Mutagen.Bethesda;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Records;
using Mutagen.Bethesda.Skyrim;
using Noggog;

namespace CardboardFaceMask.Generator;

/// <summary>Builds the in-memory CardboardFaceMask plugin from the build inputs.</summary>
public static class PluginBuilder
{
    // Local FormIDs are a contract (docs/CONTRACTS.md); ESL plugins may use 0x800..0xFFF.
    public const uint ArmorId = 0x800;
    public const uint HumanAddonId = 0x801;
    public const uint BeastAddonId = 0x802;
    public const string ArmorEditorId = "FaceMaskArmor";
    public const string HumanAddonEditorId = "FaceMaskAA";
    public const string BeastAddonEditorId = "FaceMaskAA_Beast";
    public const string ArmorName = "Cardboard Face Mask";

    /// <summary>
    /// HEDR version 1.70: valid on every Skyrim SE/AE runtime. 1.71 (game 1.6.1130+) only adds the
    /// 0x001-0x7FF light-plugin range, which this plugin does not use.
    /// </summary>
    public const float HeaderVersion = 1.7f;

    public static SkyrimMod Build(BuildInputs inputs)
    {
        var modKey = ModKey.FromFileName(inputs.PluginName);
        var mod = new SkyrimMod(modKey, SkyrimRelease.SkyrimSE, HeaderVersion);
        mod.ModHeader.Flags |= SkyrimModHeader.HeaderFlag.Small; // ESL flag (0x200)

        // The file is written by Program.CanonicalBytes (records in FormID order). Spriggit may
        // rebuild a GRUP in another order, so build_plugin.py canonicalizes its output the same way.
        var bodyTemplate = SlotTemplate(inputs.Slot);
        var human = AddAddon(mod, HumanAddonId, HumanAddonEditorId, bodyTemplate,
            Vanilla.DefaultRace, Vanilla.HumanRaces,
            inputs.Models.HumanMale, inputs.Models.HumanFemale);
        var beast = AddAddon(mod, BeastAddonId, BeastAddonEditorId, bodyTemplate,
            Vanilla.BeastPrimaryRace, Vanilla.BeastAdditionalRaces,
            inputs.Models.BeastMale, inputs.Models.BeastFemale);
        AddArmor(mod, bodyTemplate, inputs, armatureInOrder: new[] { beast, human });
        return mod;
    }

    /// <summary>BOD2 for a single biped slot: slot N is bit (N - 30); armor type Clothing.</summary>
    public static BodyTemplate SlotTemplate(int slot) => new()
    {
        FirstPersonFlags = (BipedObjectFlag)(1u << (slot - 30)),
        ArmorType = ArmorType.Clothing,
    };

    private static ArmorAddon AddAddon(SkyrimMod mod, uint id, string editorId, BodyTemplate body,
        IFormLinkGetter<IRaceGetter> race, IEnumerable<IFormLinkGetter<IRaceGetter>> additionalRaces,
        string maleModel, string femaleModel)
    {
        var addon = new ArmorAddon(new FormKey(mod.ModKey, id), SkyrimRelease.SkyrimSE)
        {
            EditorID = editorId,
            BodyTemplate = body.DeepCopy(),
            Priority = new GenderedItem<byte>(0, 0),
            // The meshes have no _0/_1 weight variants, so the weight slider must be off.
            WeightSliderEnabled = new GenderedItem<bool>(false, false),
            DetectionSoundValue = 0,
            WorldModel = new GenderedItem<Model?>(MakeModel(maleModel), MakeModel(femaleModel)),
        };
        addon.Race.SetTo(race.FormKey);
        foreach (var r in SortedByFormId(additionalRaces))
            addon.AdditionalRaces.Add(r);
        mod.ArmorAddons.Add(addon);
        return addon;
    }

    private static void AddArmor(SkyrimMod mod, BodyTemplate body, BuildInputs inputs, IEnumerable<ArmorAddon> armatureInOrder)
    {
        var armor = new Armor(new FormKey(mod.ModKey, ArmorId), SkyrimRelease.SkyrimSE)
        {
            EditorID = ArmorEditorId,
            Name = ArmorName,
            BodyTemplate = body.DeepCopy(),
            Value = 0,
            Weight = 0,
            ArmorRating = 0,
            // xEdit marks ARMO DESC as required (CK always writes it); empty for a plain item.
            Description = string.Empty,
        };
        armor.Race.SetTo(Vanilla.DefaultRace.FormKey);
        armor.PickUpSound.SetTo(Vanilla.PickUpSound.FormKey);
        armor.PutDownSound.SetTo(Vanilla.PutDownSound.FormKey);
        armor.Keywords = new ExtendedList<IFormLinkGetter<IKeywordGetter>>();
        foreach (var kw in Vanilla.ArmorKeywords)
            armor.Keywords.Add(kw);
        // Order matters: the engine uses the first addon whose race list matches the wearer,
        // so the beast addon goes first.
        foreach (var addon in armatureInOrder)
            armor.Armature.Add(addon.ToLinkGetter<IArmorAddonGetter>());
        if (inputs.NonPlayable)
            armor.MajorFlags |= Armor.MajorFlag.NonPlayable;
        if (inputs.Models.Ground != null)
            armor.WorldModel = new GenderedItem<ArmorModel?>(new ArmorModel { Model = MakeModel(inputs.Models.Ground) }, null);
        mod.Armors.Add(armor);
    }

    private static Model MakeModel(string path) => new() { File = path };

    /// <summary>
    /// xEdit defines ARMA "Additional Races" as a sorted array (wbRArrayS), i.e. the canonical
    /// on-disk order is ascending FormID; the engine only tests membership, so order is cosmetic.
    /// </summary>
    public static IReadOnlyList<IFormLinkGetter<IRaceGetter>> SortedByFormId(IEnumerable<IFormLinkGetter<IRaceGetter>> races) =>
        races.OrderBy(r => r.FormKey.ID).ToList();
}
