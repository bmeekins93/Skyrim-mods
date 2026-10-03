using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Records;
using Mutagen.Bethesda.Skyrim;

namespace CardboardFaceMask.Generator;

/// <summary>Contract assertions run on the plugin as read back from disk.</summary>
public static class Checks
{
    public static void Validate(ISkyrimModGetter mod, BuildInputs inputs)
    {
        var errors = new List<string>();
        void Expect(bool ok, string message) { if (!ok) errors.Add(message); }

        var header = mod.ModHeader;
        Expect(header.Flags.HasFlag(SkyrimModHeader.HeaderFlag.Small), "TES4 ESL (Small, 0x200) flag not set");
        Expect(!header.Flags.HasFlag(SkyrimModHeader.HeaderFlag.Master), "TES4 must not carry the ESM flag");
        var masters = header.MasterReferences.Select(m => m.Master.FileName.String).ToList();
        Expect(masters.SequenceEqual(new[] { "Skyrim.esm" }), $"masters must be exactly [Skyrim.esm], got [{string.Join(", ", masters)}]");

        var records = mod.EnumerateMajorRecords().ToList();
        Expect(records.Count == 3, $"expected exactly 3 records, found {records.Count}");
        Expect(mod.Armors.Count == 1 && mod.ArmorAddons.Count == 2, "expected exactly one ARMO and two ARMA");

        var armor = Find<IArmorGetter>(mod, PluginBuilder.ArmorId, PluginBuilder.ArmorEditorId, errors);
        var human = Find<IArmorAddonGetter>(mod, PluginBuilder.HumanAddonId, PluginBuilder.HumanAddonEditorId, errors);
        var beast = Find<IArmorAddonGetter>(mod, PluginBuilder.BeastAddonId, PluginBuilder.BeastAddonEditorId, errors);
        if (armor is null || human is null || beast is null)
            Fail(errors);

        var slotFlag = (BipedObjectFlag)(1u << (inputs.Slot - 30));
        CheckArmor(armor!, human!, beast!, slotFlag, inputs, Expect);
        CheckAddon(human!, slotFlag, Vanilla.DefaultRace, Vanilla.HumanRaces, inputs.Models.HumanMale, inputs.Models.HumanFemale, Expect);
        CheckAddon(beast!, slotFlag, Vanilla.BeastPrimaryRace, Vanilla.BeastAdditionalRaces, inputs.Models.BeastMale, inputs.Models.BeastFemale, Expect);
        if (errors.Count > 0)
            Fail(errors);
    }

    private static T? Find<T>(ISkyrimModGetter mod, uint id, string editorId, List<string> errors)
        where T : class, IMajorRecordGetter
    {
        var key = new FormKey(mod.ModKey, id);
        var rec = mod.EnumerateMajorRecords().FirstOrDefault(r => r.FormKey == key) as T;
        if (rec is null)
            errors.Add($"no {typeof(T).Name} at {key}");
        else if (rec.EditorID != editorId)
            errors.Add($"{key}: EditorID '{rec.EditorID}' != '{editorId}'");
        return rec;
    }

    private static void CheckArmor(IArmorGetter armor, IArmorAddonGetter human, IArmorAddonGetter beast,
        BipedObjectFlag slotFlag, BuildInputs inputs, Action<bool, string> expect)
    {
        expect(armor.Name?.String == PluginBuilder.ArmorName, $"ARMO FULL is '{armor.Name?.String}'");
        expect(armor.BodyTemplate?.FirstPersonFlags == slotFlag, $"ARMO BOD2 flags {armor.BodyTemplate?.FirstPersonFlags}");
        expect(armor.BodyTemplate?.ArmorType == ArmorType.Clothing, "ARMO armor type is not Clothing");
        expect(armor.Race.FormKey == Vanilla.DefaultRace.FormKey, "ARMO RNAM is not DefaultRace");
        expect(armor.Value == 0 && armor.Weight == 0 && armor.ArmorRating == 0, "ARMO value/weight/AR must all be 0");
        var order = armor.Armature.Select(a => a.FormKey).ToList();
        expect(order.SequenceEqual(new[] { beast.FormKey, human.FormKey }), "ARMO armature order must be [beast, human]");
        expect(armor.MajorFlags.HasFlag(Armor.MajorFlag.NonPlayable) == inputs.NonPlayable, "ARMO Non-Playable flag does not match config");
        var ground = armor.WorldModel?.Male?.Model?.File.GivenPath;
        expect(ground == inputs.Models.Ground, $"ARMO world model '{ground}' != manifest ground_model '{inputs.Models.Ground}'");
        expect(armor.WorldModel?.Female is null, "ARMO must not have a female world model");
    }

    private static void CheckAddon(IArmorAddonGetter addon, BipedObjectFlag slotFlag, IFormLinkGetter<IRaceGetter> race,
        IReadOnlyList<IFormLinkGetter<IRaceGetter>> extraRaces, string male, string female, Action<bool, string> expect)
    {
        string id = addon.EditorID ?? addon.FormKey.ToString();
        expect(addon.BodyTemplate?.FirstPersonFlags == slotFlag && addon.BodyTemplate?.ArmorType == ArmorType.Clothing, $"{id}: BOD2 mismatch");
        expect(addon.Race.FormKey == race.FormKey, $"{id}: RNAM mismatch");
        var extra = addon.AdditionalRaces.Select(r => r.FormKey).ToList();
        expect(extra.SequenceEqual(PluginBuilder.SortedByFormId(extraRaces).Select(r => r.FormKey)), $"{id}: additional races mismatch");
        expect(addon.WorldModel?.Male?.File.GivenPath == male, $"{id}: male model '{addon.WorldModel?.Male?.File.GivenPath}' != '{male}'");
        expect(addon.WorldModel?.Female?.File.GivenPath == female, $"{id}: female model '{addon.WorldModel?.Female?.File.GivenPath}' != '{female}'");
        expect(addon.FirstPersonModel?.Male is null && addon.FirstPersonModel?.Female is null, $"{id}: must have no first-person models");
        expect(addon.WeightSliderEnabled.Male == false && addon.WeightSliderEnabled.Female == false, $"{id}: weight slider must be disabled");
        expect(addon.Priority.Male == 0 && addon.Priority.Female == 0, $"{id}: priorities must be 0");
        expect(addon.DetectionSoundValue == 0, $"{id}: detection sound value must be 0");
    }

    private static void Fail(List<string> errors) =>
        throw new InvalidDataException("plugin contract check failed:\n  " + string.Join("\n  ", errors));
}
