using System.Reflection;
using Mutagen.Bethesda.FormKeys.SkyrimSE;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Skyrim;

namespace CardboardFaceMask.Generator;

/// <summary>
/// Every vanilla record the plugin references. All links come from Mutagen.Bethesda.FormKeys.SkyrimSE,
/// which is generated from the shipped Skyrim.esm, so FormIDs and EditorIDs are not guessed.
/// tests/test_plugin.py cross-checks the same values against an independent dump.
/// </summary>
public static class Vanilla
{
    /// <summary>
    /// Races that wear the human mask (ARMA FaceMaskAA). All are adult, use the human character
    /// skeleton/head ("NPC Head [Head]") and are defined in Skyrim.esm (single master, no DLC races).
    /// Deliberately excluded: the *RaceChild* races (children), ManakinRace (player-home display
    /// mannequins, not NPCs), InvisibleRace (no visible body), creature/undead skeleton races
    /// (Draugr, DragonPriest, Falmer, Hagraven, Giant, Werewolf, ...). SnowElfRace lives in
    /// Dawnguard.esm, not Skyrim.esm, so it cannot be referenced with a single master.
    /// </summary>
    public static readonly IFormLinkGetter<IRaceGetter>[] HumanRaces =
    {
        Skyrim.Race.NordRace,
        Skyrim.Race.BretonRace,
        Skyrim.Race.ImperialRace,
        Skyrim.Race.RedguardRace,
        Skyrim.Race.HighElfRace,
        Skyrim.Race.DarkElfRace,
        Skyrim.Race.WoodElfRace,
        Skyrim.Race.OrcRace,
        Skyrim.Race.NordRaceVampire,
        Skyrim.Race.BretonRaceVampire,
        Skyrim.Race.ImperialRaceVampire,
        Skyrim.Race.RedguardRaceVampire,
        Skyrim.Race.HighElfRaceVampire,
        Skyrim.Race.DarkElfRaceVampire,
        Skyrim.Race.WoodElfRaceVampire,
        Skyrim.Race.OrcRaceVampire,
        Skyrim.Race.ElderRace,
        Skyrim.Race.ElderRaceVampire,
        // Human-skeleton special races with ActorTypeNPC: burnt Astrid (Dark Brotherhood),
        // Peryite's Afflicted (Breton-based), Dremora (humanoid Daedra, character skeleton).
        Skyrim.Race.NordRaceAstrid,
        Skyrim.Race.DA13AfflictedRace,
        Skyrim.Race.DremoraRace,
    };

    /// <summary>
    /// Primary race of the beast addon. The engine matches an addon if the actor's race is the
    /// addon's RNAM or any additional race, so Khajiit vs Argonian here is functionally equivalent;
    /// KhajiitRace follows the project spec and the other three beast races go in the extra list.
    /// </summary>
    public static readonly IFormLinkGetter<IRaceGetter> BeastPrimaryRace = Skyrim.Race.KhajiitRace;

    public static readonly IFormLinkGetter<IRaceGetter>[] BeastAdditionalRaces =
    {
        Skyrim.Race.ArgonianRace,
        Skyrim.Race.KhajiitRaceVampire,
        Skyrim.Race.ArgonianRaceVampire,
    };

    /// <summary>RNAM of the ARMO and the human ARMA, as on vanilla armour.</summary>
    public static readonly IFormLinkGetter<IRaceGetter> DefaultRace = Skyrim.Race.DefaultRace;

    /// <summary>
    /// ArmorClothing: the item-type keyword matching BOD2 armor type "Clothing" (vanilla clothing
    /// carries it; perks/mods classify worn gear by it). VendorItemClothing is left out on purpose:
    /// the mask is worth 0 gold, so there is nothing to trade.
    /// </summary>
    public static readonly IFormLinkGetter<IKeywordGetter>[] ArmorKeywords =
    {
        Skyrim.Keyword.ArmorClothing,
    };

    /// <summary>Vanilla clothing pick-up / put-down sounds, so looting the mask is not silent.</summary>
    public static readonly IFormLinkGetter<ISoundDescriptorGetter> PickUpSound = Skyrim.SoundDescriptor.ITMClothingUpSD;
    public static readonly IFormLinkGetter<ISoundDescriptorGetter> PutDownSound = Skyrim.SoundDescriptor.ITMClothingDownSD;

    private static readonly Lazy<Dictionary<FormKey, string>> Names = new(BuildNameTable);

    /// <summary>
    /// Machine-readable record of every vanilla FormKey the plugin uses (EditorID -> FormKey, in
    /// on-disk order). Written to src/plugin/vanilla_formkeys.json; tests/test_plugin.py checks the
    /// ESP against it and checks it against an independent dump of Skyrim.esm.
    /// </summary>
    public static string ToJson()
    {
        static Dictionary<string, string> Table<T>(IEnumerable<IFormLinkGetter<T>> links)
            where T : class, Mutagen.Bethesda.Plugins.Records.IMajorRecordGetter
            => links.ToDictionary(l => NameOf(l.FormKey), l => l.FormKey.ToString());

        var package = typeof(Skyrim).Assembly.GetName();
        var doc = new Dictionary<string, object>
        {
            ["source"] = $"{package.Name} {package.Version} (generated from Skyrim.esm)",
            ["armor"] = new Dictionary<string, object>
            {
                ["race"] = Table(new[] { DefaultRace }),
                ["keywords"] = Table(ArmorKeywords),
                ["pickup_sound"] = Table(new[] { PickUpSound }),
                ["putdown_sound"] = Table(new[] { PutDownSound }),
            },
            ["human_addon"] = new Dictionary<string, object>
            {
                ["race"] = Table(new[] { DefaultRace }),
                ["additional_races"] = Table(PluginBuilder.SortedByFormId(HumanRaces)),
            },
            ["beast_addon"] = new Dictionary<string, object>
            {
                ["race"] = Table(new[] { BeastPrimaryRace }),
                ["additional_races"] = Table(PluginBuilder.SortedByFormId(BeastAdditionalRaces)),
            },
        };
        var options = new System.Text.Json.JsonSerializerOptions { WriteIndented = true };
        return System.Text.Json.JsonSerializer.Serialize(doc, options).ReplaceLineEndings("\n") + "\n";
    }

    /// <summary>EditorID of a vanilla FormKey from the FormKeys package (property names are EditorIDs).</summary>
    public static string NameOf(FormKey key) => Names.Value.TryGetValue(key, out var n) ? n : "?";

    private static Dictionary<FormKey, string> BuildNameTable()
    {
        var table = new Dictionary<FormKey, string>();
        foreach (var holder in new[] { typeof(Skyrim.Race), typeof(Skyrim.Keyword), typeof(Skyrim.SoundDescriptor) })
        {
            foreach (var prop in holder.GetProperties(BindingFlags.Public | BindingFlags.Static))
            {
                if (prop.GetValue(null) is IFormLinkIdentifier link)
                    table[link.FormKey] = prop.Name;
            }
        }
        return table;
    }
}
