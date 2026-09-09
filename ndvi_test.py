import ee

# ============================================================
# AGRIVISION - REAL SENTINEL-2 NDVI TEST
# ============================================================

# Connect to Google Earth Engine
ee.Initialize(project="agrivision-481416")

print("✅ Earth Engine connected!")

# ============================================================
# FARM LOCATION
# ============================================================

latitude = float(input("Enter farm latitude: "))
longitude = float(input("Enter farm longitude: "))

point = ee.Geometry.Point([
    longitude,
    latitude
])

# 500 metre area around the selected location
farm_area = point.buffer(500)

print()
print("📍 Farm location:")
print("Latitude :", latitude)
print("Longitude:", longitude)
print()

# ============================================================
# SENTINEL-2 DATA
# ============================================================

images = (
    ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
    .filterBounds(farm_area)
    .filterDate("2025-08-01", "2026-08-12")
    .filter(
        ee.Filter.lt(
            "CLOUDY_PIXEL_PERCENTAGE",
            80
        )
    )
)

# Count images
image_count = images.size().getInfo()

print("🛰️ Sentinel-2 images found:", image_count)

# ============================================================
# CHECK IMAGES
# ============================================================

if image_count == 0:

    print()
    print("❌ No Sentinel-2 images found.")
    print("Try another location or date range.")

else:

    print("✅ Sentinel-2 data available!")

    # ========================================================
    # CREATE MEDIAN IMAGE
    # ========================================================

    image = images.median()

    # ========================================================
    # NDVI
    #
    # Sentinel-2:
    # B8 = Near Infrared
    # B4 = Red
    #
    # NDVI = (NIR - RED) / (NIR + RED)
    # ========================================================

    ndvi = image.normalizedDifference(
        ["B8", "B4"]
    ).rename("NDVI")

    # ========================================================
    # CALCULATE AVERAGE NDVI
    # ========================================================

    result = ndvi.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=farm_area,
        scale=10,
        maxPixels=1_000_000
    )

    ndvi_value = result.get("NDVI").getInfo()

    print()
    print("🌱 =================================")
    print("          AGRIVISION NDVI")
    print("🌱 =================================")

    if ndvi_value is None:

        print("⚠️ NDVI value could not be calculated.")

    else:

        print(
            "Average NDVI:",
            round(ndvi_value, 3)
        )

        # ====================================================
        # AGRIVISION NDVI CLASSIFICATION
        # ====================================================

        if ndvi_value <= 0.20:

            health = "Very Low 🔴"

        elif ndvi_value <= 0.35:

            health = "Low 🟠"

        elif ndvi_value <= 0.50:

            health = "Moderate 🟡"

        elif ndvi_value <= 0.70:

            health = "High 🟢"

        else:

            health = "Very High 🟢"

        print("Crop Health:", health)

    print()
    print("✅ Real Sentinel-2 NDVI calculation completed!")